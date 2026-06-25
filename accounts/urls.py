from django.urls import path
from . import views


urlpatterns = [
    path("", views.login_view, name="login"),
    path("home/", views.home, name="home"),
    path("truncate-records/", views.truncate_records, name="truncate_records"),
    path("logout/", views.logout_view, name="logout"),
    path("users/", views.user_list, name="user_list"),
    path("users/add/", views.add_user, name="add_user"),
    path("users/edit/<int:user_id>/", views.edit_user, name="edit_user"),
    path("users/delete/<int:user_id>/", views.delete_user, name="delete_user"),
    path("manage-account/", views.manage_account, name="manage_account"),
    # Accounts (unified farmer + trader)
    path("accounts/", views.account_hub, name="account_hub"),
    path("farmers/", views.farmer_list, name="farmer_list"),
    path("farmers/add/", views.add_farmer, name="add_farmer"),
    path("farmers/edit/<int:farmer_id>/", views.edit_farmer, name="edit_farmer"),
    path("farmers/delete/<int:farmer_id>/", views.delete_farmer, name="delete_farmer"),
    # Traders
    path("traders/", views.trader_list, name="trader_list"),
    path("traders/add/", views.add_trader, name="add_trader"),
    path("traders/edit/<int:trader_id>/", views.edit_trader, name="edit_trader"),
    path("traders/delete/<int:trader_id>/", views.delete_trader, name="delete_trader"),
    # Avak
    path("avak/", views.avak_list, name="avak_list"),
    path("avak/add/", views.add_avak, name="add_avak"),
    path("avak/edit/<int:avak_id>/", views.edit_avak, name="edit_avak"),
    path("avak/view/<int:avak_id>/", views.view_avak, name="view_avak"),
    path("avak/view-all/", views.view_all_avak, name="view_all_avak"),
    path("avak/tender-form/", views.tender_form, name="tender_form"),
    path("avak/delete/<int:avak_id>/", views.delete_avak, name="delete_avak"),
    # APIs
    path("api/get-farmers/", views.get_farmers, name="get_farmers"),
    path("api/get-traders/", views.get_traders, name="get_traders"),
    path("api/get-trader-details/", views.get_trader_details, name="get_trader_details"),
    path("api/get-created-bills/", views.get_created_bills, name="get_created_bills"),
    path("api/get-places/", views.get_places, name="get_places"),
    path("api/check-lot-number/", views.check_lot_number, name="check_lot_number"),
    path("api/get-next-lot-number/", views.get_next_lot_number, name="get_next_lot_number"),
    path("api/get-bikri-last-lot/", views.get_bikri_last_lot, name="get_bikri_last_lot"),
    path("api/get-lot-details/", views.get_lot_details, name="get_lot_details"),
    path("api/get-farmer-details/", views.get_farmer_details, name="get_farmer_details"),

    # Bikri
    path("bikri/", views.bikri_list, name="bikri_list"),
    path("bikri/add/", views.add_bikri, name="add_bikri"),
    path("bikri/edit/<int:bikri_id>/", views.edit_bikri, name="edit_bikri"),
    path("bikri/edit-multi/<int:bikri_id>/", views.edit_bikri_multi, name="edit_bikri_multi"),
    path("bikri/view/<int:bikri_id>/", views.view_bikri, name="view_bikri"),
    path("bikri/delete/<int:bikri_id>/", views.delete_bikri, name="delete_bikri"),
    path("api/next-bill-no/", views.get_next_bill_no, name="get_next_bill_no"),
    # Market Rates
    path("market-rates/", views.market_rates, name="market_rates"),
    path("bank-master/", views.bank_master, name="bank_master"),
    path("company-settings/", views.company_settings, name="company_settings"),
    path("api/get-market-rates/", views.get_market_rates, name="get_market_rates"),
    # Reports
    path("delivery-book/", views.delivery_book, name="delivery_book"),
    path("akada/", views.akada, name="akada"),
    path("chopada/", views.chopada, name="chopada"),
    path("pategalu/", views.pategalu, name="pategalu"),
    path("gst-reports/", views.gst_reports, name="gst_reports"),
    path("monthwise_gst_report/", views.monthwise_gst_report, name="monthwise_gst_report"),
    path("detailed_gst_report/", views.detailed_gst_report, name="detailed_gst_report"),
    path("cess_report/", views.cess_report, name="cess_report"),
    path("weekly_cess_report/", views.weekly_cess_report, name="weekly_cess_report"),
    path("gstr1_report/", views.gstr1_report, name="gstr1_report"),
    path("partywise_gst_report/", views.partywise_gst_report, name="partywise_gst_report"),
    path("bazar-kharidi/", views.bazar_kharidi, name="bazar_kharidi"),
    path("nondha/", views.nondha, name="nondha"),
    # Administrative Tools
    path("edit-cancel/", views.edit_cancel_dashboard, name="edit_cancel_dashboard"),
    path("api/get-traders-by-date/", views.get_traders_by_date, name="get_traders_by_date"),
    path("api/get-lots-by-date/", views.get_lots_by_date, name="get_lots_by_date"),
    path("api/get-bills-by-date/", views.get_bills_by_date, name="get_bills_by_date"),
    path("api/transfer-all-lots/", views.transfer_all_lots, name="transfer_all_lots"),
    path("api/transfer-lot-wise/", views.transfer_lot_wise, name="transfer_lot_wise"),
    path("api/update-farmer-details/", views.update_farmer_details, name="update_farmer_details"),
    path("api/cancel-vikri-patti/", views.cancel_vikri_patti, name="cancel_vikri_patti"),
    path("api/cancel-kharidi-patti/", views.cancel_kharidi_patti, name="cancel_kharidi_patti"),
    # Kharidi Patti (New)
    path("kharidi-patti/", views.kharidi_patti, name="kharidi_patti"),
    path("kharidi-patti-list/", views.kharidi_patti_list, name="kharidi_patti_list"),
    path("kharidi-patti/view/<int:bill_id>/", views.view_trader_bill, name="view_trader_bill"),
    path("kharidi-patti/delete/<int:bill_id>/", views.delete_trader_bill, name="delete_trader_bill"),
    path("api/get-buyer-lots/", views.get_buyer_lots, name="get_buyer_lots"),
    path("api/save-trader-bill/", views.save_trader_bill, name="save_trader_bill"),
    path("api/update-trader-details/", views.update_trader_details, name="update_trader_details"),
    path("api/transfer-trader-lots/", views.transfer_trader_lots, name="transfer_trader_lots"),
    path("lot-detail-modification/", views.lot_detail_modification, name="lot_detail_modification"),
    path("api/get-lot-bags-details/", views.get_lot_bags_details, name="get_lot_bags_details"),
    path("api/update-avak-tender/", views.update_avak_tender, name="update_avak_tender"),
    path("api/upload-tender-pdf/", views.upload_tender_pdf, name="upload_tender_pdf"),
    path("api/validate-tender-pdf-rows/", views.validate_tender_pdf_rows, name="validate_tender_pdf_rows"),
    path("api/confirm-tender-pdf-import/", views.confirm_tender_pdf_import, name="confirm_tender_pdf_import"),
    path("api/save-tender-rates/", views.save_tender_rates, name="save_tender_rates"),
    path("api/transfer-bag-weights/", views.transfer_bag_weights, name="transfer_bag_weights"),
    # Accounts
    path("account-statement/", views.account_statement, name="account_statement"),
    path("payments/", views.payment_list, name="payment_list"),
    path("payments/add/", views.add_payment, name="add_payment"),
    path("receipts/", views.receipt_list, name="receipt_list"),
    path("receipts/add/", views.add_receipt, name="add_receipt"),
    path("farmer-ledger/", views.farmer_ledger, name="farmer_ledger"),
    path("trader-ledger/", views.trader_ledger, name="trader_ledger"),

    # Transaction Actions
    path("accounts/transaction/edit/<int:transaction_id>/", views.edit_financial_transaction, name="edit_transaction"),
    path("accounts/transaction/delete/<int:transaction_id>/", views.delete_financial_transaction, name="delete_transaction"),
    path("accounts/transaction/view/<int:transaction_id>/", views.view_financial_transaction, name="view_transaction"),

    # ── Tally Voucher System ──────────────────────────────────────────────────
    # Ledger Master
    path("ledger-master/", views.ledger_master, name="ledger_master"),
    path("ledger-master/add/", views.add_ledger_account, name="add_ledger_account"),
    path("ledger-master/edit/<int:ledger_id>/", views.edit_ledger_account, name="edit_ledger_account"),
    path("ledger-master/delete/<int:ledger_id>/", views.delete_ledger_account, name="delete_ledger_account"),
    path("grouping-master/", views.grouping_master, name="grouping_master"),
    path("grouping-master/add/", views.add_ledger_group, name="add_ledger_group"),
    path("grouping-master/edit/<int:group_id>/", views.edit_ledger_group, name="edit_ledger_group"),
    path("grouping-master/delete/<int:group_id>/", views.delete_ledger_group, name="delete_ledger_group"),
    # Voucher Entry
    path("vouchers/", views.voucher_list, name="voucher_list"),
    path("vouchers/add/", views.add_voucher, name="add_voucher"),
    path("vouchers/view/<int:voucher_id>/", views.view_voucher, name="view_voucher"),
    path("vouchers/edit/<int:voucher_id>/", views.edit_voucher, name="edit_voucher"),
    path("vouchers/delete/<int:voucher_id>/", views.delete_voucher, name="delete_voucher"),
    # Auto-voucher from Vikri Patti
    path("vouchers/from-bikri/<int:bikri_id>/", views.create_voucher_from_bikri, name="voucher_from_bikri"),
    # Auto-voucher from Kharidi Patti (Trader Bill)
    path("vouchers/from-trader-bill/<int:bill_id>/", views.create_voucher_from_trader_bill, name="voucher_from_trader_bill"),
    # Ledger Book (account statement)
    path("ledger-book/", views.ledger_book, name="ledger_book"),
    path("ledger-book/monthwise/", views.ledger_monthwise, name="ledger_monthwise"),
    path("trial-balance/", views.trial_balance, name="trial_balance"),
    path("profit-and-loss/", views.profit_and_loss, name="profit_and_loss"),
    # API
    path("api/get-ledger-accounts/", views.api_get_ledger_accounts, name="api_get_ledger_accounts"),

    # API
    path("api/search-bikri/", views.search_bikri, name="search_bikri"),

    # ── Tally Voucher Entry (Farmer + Trader combined) ──────────────────────
    path("tally/payment/",         views.tally_payment_view, name="tally_payment"),
    path("tally/payment/list/",    views.tally_payment_list, name="tally_payment_list"),
    path("tally/receipt/",         views.tally_receipt_view, name="tally_receipt"),
    path("tally/receipt/list/",    views.tally_receipt_list, name="tally_receipt_list"),

    # ── Unified / Consolidated Account Statement ────────────────────────────
    path("unified-ledger/",        views.unified_ledger,     name="unified_ledger"),
]

