# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class HrPayslipRegisterPaymentWizard(models.TransientModel):
    _name = "hr.payslip.register.payment.wizard"
    _description = "Payslip Register Payment wizard"

    def _get_active_payslip(self):
        context = dict(self._context or {})
        active_ids = context.get('active_ids', [])
        return self.env['hr.payslip'].browse(active_ids)

    @api.model
    def _default_partner_id(self):
        payslip = self._get_active_payslip()
        return payslip.employee_id.address_home_id.id

    def _default_currency_id(self):
        return self.env.user.company_id.currency_id.id

    currency_id = fields.Many2one('res.currency', string='Currency', required=True,
                                  default=_default_currency_id)
    partner_id = fields.Many2one('res.partner', string='Partner', required=True, default=_default_partner_id)
    journal_id = fields.Many2one('account.journal', string='Payment Method', required=True,
                                 domain=[('type', 'in', ('bank', 'cash'))])
    company_id = fields.Many2one('res.company', related='journal_id.company_id', string='Company', readonly=True,
                                 required=True)
    payment_method_id = fields.Many2one('account.payment.method', string='Payment Type', required=True)
    amount = fields.Monetary(string='Payment Amount',
                             currency_field='currency_id',
                             required=True)
    amount_residual = fields.Monetary(string='Residual Amount',
                                      currency_field='currency_id',
                                      readonly=True)
    payment_date = fields.Date(string='Payment Date', default=fields.Date.context_today, required=True)
    communication = fields.Char(string='Memo')
    hide_payment_method = fields.Boolean(compute='_compute_hide_payment_method',
                                         help="Technical field used to hide the payment method if the selected journal has only one available which is 'manual'")

    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        if self.journal_id and self.journal_id.currency_id:
            self.currency_id = self.journal_id.currency_id
        else:
            self.currency_id = self.env.user.company_id.currency_id

    @api.onchange('currency_id', 'payment_date')
    def _onchange_currency_id(self):
        payslip = self._get_active_payslip()
        self.amount = self._get_amount(payslip.residual_company_signed)

    def _get_amount(self, amount):
        if self.currency_id:
            return self.env.user.company_id.currency_id.with_context(date=self.payment_date).compute(amount,
                                                                                                     self.currency_id)
        return amount

    @api.onchange('amount', 'currency_id', 'payment_date')
    def _update_residual(self):
        payslip = self._get_active_payslip()
        self.amount_residual = self._get_amount(payslip.residual_company_signed) - self.amount

    @api.one
    @api.constrains('amount')
    def _check_amount(self):
        if not self.amount > 0.0:
            raise ValidationError(_('The payment amount must be strictly positive.'))

    @api.one
    @api.depends('journal_id')
    def _compute_hide_payment_method(self):
        if not self.journal_id:
            self.hide_payment_method = True
            return
        journal_payment_methods = self.journal_id.outbound_payment_method_ids
        self.hide_payment_method = len(journal_payment_methods) == 1 and journal_payment_methods[0].code == 'manual'

    @api.onchange('journal_id')
    def _onchange_journal(self):
        if self.journal_id:
            # Set default payment method (we consider the first to be the default one)
            payment_methods = self.journal_id.outbound_payment_method_ids
            self.payment_method_id = payment_methods and payment_methods[0] or False
            # Set payment method domain (restrict to methods enabled for the journal and to selected payment type)
            return {
                'domain': {'payment_method_id': [('payment_type', '=', 'outbound'), ('id', 'in', payment_methods.ids)]}}
        return {}

    def _get_payment_vals(self):
        """ Hook for extension """
        return {
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'partner_id': self.partner_id.id,
            'journal_id': self.journal_id.id,
            'company_id': self.company_id.id,
            'payment_method_id': self.payment_method_id.id,
            'amount': self.amount,
            'currency_id': self.currency_id.id,
            'payment_date': self.payment_date,
            'communication': self.communication,
            'writeoff_label': 'Payslip Payment',
            'payslip_id': self._get_active_payslip().id
        }

    @api.multi
    def expense_post_payment(self):
        self.ensure_one()
        # Create payment and post it
        payment = self.env['account.payment'].create(self._get_payment_vals())
        payment.post()

        return {'type': 'ir.actions.act_window_close'}
