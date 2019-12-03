import threading

from PyQt5.QtCore import Qt, pyqtSignal, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QWidget, QGridLayout, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QProgressDialog
)

from electrumsv import bitcoin
from electrumsv.app_state import app_state
from electrumsv.i18n import _
from electrumsv.logs import logs
from electrumsv.networks import Net

from . import util

logger = logs.get_logger("coinsplitting")

TX_DESC_PREFIX = _("ElectrumSV coin splitting")

RESULT_DUST_TIMEOUT = -2
RESULT_DIALOG_CLOSED = -1
RESULT_READY_FOR_SPLIT = 0

STAGE_INACTIVE = -1
STAGE_PREPARING = 0
STAGE_OBTAINING_DUST = 1
STAGE_SPLITTING = 2

STAGE_NAMES = {
    STAGE_INACTIVE: _("Inactive") +".",
    STAGE_PREPARING: _("Preparing") +"..",
    STAGE_OBTAINING_DUST: _("Obtaining dust") +"..",
    STAGE_SPLITTING: _("Splitting coins") +"..",
}

class CoinSplittingTab(QWidget):
    receiving_address = None
    unfrozen_balance = None
    frozen_balance = None
    split_stage = STAGE_INACTIVE
    faucet_status_code = None

    intro_label = None
    splittable_balance_label = None
    unsplittable_balance_label = None
    splittable_unit_label = None
    unsplittable_unit_label = None
    waiting_dialog = None
    new_transaction_cv = None
    split_button = None

    def _on_split_button_clicked(self):
        self.split_button.setText(_("Splitting") +"...")
        self.split_button.setEnabled(False)

        window = self.window()
        self.receiving_address = window.wallet.get_unused_address()
        self.split_stage = STAGE_PREPARING
        self.new_transaction_cv = threading.Condition()

        window.network.register_callback(self._on_network_event, ['new_transaction'])
        self.waiting_dialog = SplitWaitingDialog(window, self, self._split_prepare_task,
            on_done=self._on_split_prepare_done, on_cancel=self._on_split_abort)

    def _split_prepare_task(self, our_dialog: 'SplitWaitingDialog'):
        self.split_stage = STAGE_OBTAINING_DUST

        wallet = self.window().wallet
        wallet.set_frozen_state([ self.receiving_address ], True)

        address_text = self.receiving_address.to_string()
        QDesktopServices.openUrl(QUrl("{}/?addr={}".format(Net.FAUCET_URL, address_text)))

        # Wait for the transaction to arrive.  How long it takes before the progress bar
        # stalls (should easily cover normal expected time required).
        max_time_passed_for_progress = 40.0
        # How long to wait before failing the process.
        max_time_passed_for_failure = 120.0
        was_received = False
        with self.new_transaction_cv:
            time_passed = 0.0
            while not was_received:
                if our_dialog != self.waiting_dialog:
                    return RESULT_DIALOG_CLOSED
                if time_passed >= max_time_passed_for_failure:
                    return RESULT_DUST_TIMEOUT
                self.waiting_dialog.set_stage_progress(time_passed/max_time_passed_for_progress)
                was_received = self.new_transaction_cv.wait(0.1)
                time_passed += 0.1

        # The user needs to sign the transaction.  It can't be done in this thread.
        wallet.set_frozen_state([ self.receiving_address ], False)
        self.split_stage = STAGE_SPLITTING
        return RESULT_READY_FOR_SPLIT

    def _on_split_abort(self):
        window = self.window()
        window.show_error(_("Coin-splitting process has been cancelled."))
        self._cleanup_tx_final()
        self._cleanup_tx_created()

    def _on_split_prepare_done(self, future):
        window = self.window()
        try:
            result = future.result()
        except Exception as exc:
            window.on_exception(exc)
        else:
            if result == RESULT_READY_FOR_SPLIT:
                self._ask_send_split_transaction()
                return

            if result == RESULT_DIALOG_CLOSED:
                window.show_error(_("Coin-splitting process has been cancelled."))
            elif result == RESULT_DUST_TIMEOUT:
                window.show_error(_("It took too long to get the dust from the faucet."))
            else:
                window.show_error(_("Unexpected situation. You should not even be here."))
            self._cleanup_tx_final()
        finally:
            self._cleanup_tx_created()

    def _ask_send_split_transaction(self):
        window = self.window()
        wallet = window.wallet

        unused_address = window.wallet.get_unused_address()
        outputs = [
            (bitcoin.TYPE_ADDRESS, unused_address, "!")
        ]
        coins = wallet.get_utxos(None, exclude_frozen=True, mature=True, confirmed_only=False)
        # Verify that our dust receiving address is in the available UTXOs, if it isn't, the
        # process has failed in some unexpected way.
        for coin in coins:
            if coin['address'] == self.receiving_address:
                break
        else:
            window.show_error(_("Error accessing dust coins for correct splitting."))
            self._cleanup_tx_final()
            return
        tx = wallet.make_unsigned_transaction(coins, outputs, window.config)

        amount = tx.output_value()
        fee = tx.get_fee()

        msg = [
            _("Amount to be sent") + ": " + window.format_amount_and_units(amount),
            _("Mining fee") + ": " + window.format_amount_and_units(fee),
        ]

        if wallet.has_password():
            msg.append("")
            msg.append(_("Enter your password to proceed"))
            password = window.password_dialog('\n'.join(msg))
        else:
            msg.append(_('Proceed?'))
            password = None
            if not window.question('\n'.join(msg)):
                self._cleanup_tx_final()
                return

        def sign_done(success):
            if success:
                if not tx.is_complete():
                    dialog = self.window().show_transaction(tx)
                    dialog.exec()
                else:
                    extra_text = _("Your split coins")
                    window.broadcast_transaction(tx, f"{TX_DESC_PREFIX}: {extra_text}",
                                                success_text=_("Your coins have now been split."))
            self._cleanup_tx_final()
        window.sign_tx_with_password(tx, sign_done, password)

    def _cleanup_tx_created(self):
        window = self.window()
        window.network.unregister_callback(self._on_network_event)

        # This may have already been done, given that we want our split to consider the dust
        # usabel.
        window.wallet.set_frozen_state([ self.receiving_address ], False)

        self.receiving_address = None
        self.waiting_dialog = None
        self.faucet_status_code = None
        self.split_stage = STAGE_INACTIVE

    def _cleanup_tx_final(self):
        logger.debug("final cleanup performed")
        self.split_button.setText(_("Split"))
        self.split_button.setEnabled(True)

    def _on_network_event(self, event, *args):
        window = self.window()
        if event == 'new_transaction':
            tx, wallet = args
            if wallet == window.wallet: # filter out tx's not for this wallet
                our_string = self.receiving_address.to_string()
                for tx_output in tx.outputs():
                    if tx_output[1].to_string() == our_string:
                        extra_text = _("Dust from BSV faucet")
                        wallet.set_label(tx.txid(), f"{TX_DESC_PREFIX}: {extra_text}")
                        break

                # Notify the progress dialog task thread.
                with self.new_transaction_cv:
                    self.new_transaction_cv.notify()

    def update_balances(self):
        window = self.window()
        wallet = window.wallet

        self.unfrozen_balance = wallet.get_balance(exclude_frozen_coins=True,
                                                   exclude_frozen_addresses=True)
        self.frozen_balance = wallet.get_frozen_balance()

        unfrozen_confirmed, unfrozen_unconfirmed, _unfrozen_unmature = self.unfrozen_balance
        _frozen_confirmed, _frozen_unconfirmed, _frozen_unmature = self.frozen_balance

        splittable_amount = unfrozen_confirmed + unfrozen_unconfirmed
        # unsplittable_amount = unfrozen_unmature + frozen_confirmed + frozen_unconfirmed
        # + frozen_unmature

        splittable_amount_text = window.format_amount(splittable_amount)
        unit_text = app_state.base_unit()

        text = [
            "<p>",
            _("As of the November 2018 hard-fork, Bitcoin Cash split into Bitcoin ABC "
              "and Bitcoin SV."),
            " ",
            _("This tab allows you to easily split the available coins in this wallet "
              "(approximately {} {}) on the Bitcoin SV chain.".format(
                  splittable_amount_text, unit_text)),
            " ",
            _("This will involve the following steps if you choose to proceed:"),
            "</p>",
            "<ol>",
            "<li>",
            _("Your browser will open to a faucet that can provide you with a small amount of SV "
            "coin. Once you have operated the faucet, and obtained it, ElectrumSV will "
            "detect it."),
            "</li>",
            "<li>",
            _("A transaction will be constructed including your entire spendable balance "
              "combined with the new known SV coin from the faucet, to be sent back into "
              "this wallet."),
            "</li>",
        ]
        if wallet.has_password():
            text.extend([
                "<li>",
                _("As this wallet is password protected, you will be prompted to "
                  "enter your password to sign the transaction."),
                "</li>",
            ])
        text.extend([
            "<li>",
            _("The transaction will then be broadcast, and immediately added to your "
              "wallet history so you can see it confirmed. It will be labeled as splitting "
              "related, so you can easily identify it."),
            "</li>",
            "<li>",
            _("You can then open Electron Cash and move your ABC coins to a different address, "
              "in order to finalise the split."),
            "</li>",
            "</ol>",
            "<p>",
            _("<b>This will only split the coins currently available in this wallet.</b> "
              "While any further coins you send to your wallet are included in the overall "
              "balance, if they were unsplit before sending, they remain unsplit on arrival. "
              "It it your responsibility to ensure you know if you are sending unsplit coins "
              "and what the repercussions are. If in doubt, click split and be sure."),
            "</p>",
        ])

        self.intro_label.setText("".join(text))

    def update_layout(self):
        disabled_text = None
        window = self.window()
        if hasattr(window, "wallet"):
            if window.wallet.is_deterministic():
                grid = QGridLayout()
                grid.setColumnStretch(0, 1)
                grid.setColumnStretch(4, 1)

                self.intro_label = QLabel("")
                self.intro_label.setTextFormat(Qt.RichText)
                self.intro_label.setMinimumWidth(600)
                self.intro_label.setWordWrap(True)

                self.split_button = QPushButton(_("Split"))
                self.split_button.setMaximumWidth(120)
                self.split_button.clicked.connect(self._on_split_button_clicked)

                help_content = "".join([
                    "<ol>",
                    "<li>",
                    _("Frozen coins will not be included in any split you make. You can use the "
                    "Coins tab to freeze or unfreeze selected coins, and by doing so only split "
                    "chosen amounts of your coins at a time.  The View menu can be used to toggle "
                    "tabs."),
                    "</li>",
                    "<li>",
                    _("In order to prevent abuse, the faucet will limit how often you can obtain "
                    "dust to split with. But that's okay, you can wait and split more coins. "
                    "Or, if you are not concerned with your coins being linked, you can split "
                    "dust from your already split coins, and use that to split further subsets."),
                    "</li>",
                    "</ol>",
                ])

                button_row = QHBoxLayout()
                button_row.addWidget(self.split_button)
                button_row.addWidget(util.HelpButton(help_content, textFormat=Qt.RichText,
                                                    title="Additional Information"))

                grid.addWidget(self.intro_label, 0, 1, 1, 3)
                # grid.addWidget(balance_widget, 2, 1, 1, 3, Qt.AlignHCenter)
                grid.addLayout(button_row, 2, 1, 1, 3, Qt.AlignHCenter)

                vbox = QVBoxLayout()
                vbox.addStretch(1)
                vbox.addLayout(grid)
                vbox.addStretch(1)

                self.update_balances()
            else:
                disabled_text = _("This is not the type of wallet that generate new addresses, "+
                        "and therefore it cannot be used for <br/>coin-splitting. Create a new "+
                        "standard wallet in ElectrumSV and send your coins there, then<br/>split "+
                        "them.")
        else:
            disabled_text = _("Wallet not loaded, change tabs and come back.")

        if disabled_text is not None:
            label = QLabel(disabled_text)

            hbox = QHBoxLayout()
            hbox.addWidget(label, 0, Qt.AlignHCenter | Qt.AlignVCenter)

            vbox = QVBoxLayout()
            vbox.addLayout(hbox )

        # If the tab is already laid out, it's current layout needs to be
        # reparented/removed before we can replace it.
        existingLayout = self.layout()
        if existingLayout:
            QWidget().setLayout(existingLayout)
        self.setLayout(vbox)


class SplitWaitingDialog(QProgressDialog):
    update_signal = pyqtSignal()
    update_label = None
    was_rejected = False

    def __init__(self, parent, splitter, func, on_done, on_cancel):
        self.splitter = splitter

        # These flags remove the close button, which removes a corner case that we'd
        # otherwise have to handle.
        super().__init__("", None, 0, 100, parent,
                         Qt.Window | Qt.WindowTitleHint) # | Qt.CustomizeWindowHint)

        self.setWindowModality(Qt.WindowModal)
        self.setWindowTitle(_("Please wait"))

        self.stage_progress = 0

        def _on_done(future):
            if self.was_rejected:
                return
            self.accept()
            on_done(future)
        future = app_state.app.run_in_thread(func, self, on_done=_on_done)
        self.accepted.connect(future.cancel)
        def _on_rejected():
            self.was_rejected = True
            future.cancel()
            on_cancel()
        self.rejected.connect(_on_rejected)
        self.update_signal.connect(self.update)
        self.update()
        self.show()

    def set_stage_progress(self, stage_progress):
        self.stage_progress = max(0, min(0.99, stage_progress))
        self.update_signal.emit()

    def update(self):
        self.setValue(max(1, int(self.stage_progress * 100)))
        update_text = STAGE_NAMES[self.splitter.split_stage]
        if self.update_label is None:
            self.update_label = QLabel(update_text)
            self.setLabel(self.update_label)
        else:
            self.update_label.setText(update_text)
