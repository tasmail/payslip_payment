# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models, _

from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


class HrPayslip(models.Model):
    _name = 'hr.payslip'
    _inherit = ['hr.payslip', 'mail.thread']

    def _get_default_currency_id(self):
        return self.env.user.company_id.currency_id.id

    state = fields.Selection([
        ('draft', _('Draft')),
        ('verify', _('Waiting')),
        ('done', _('Done')),
        ('paid', _('Paid')),
        ('cancel', _('Rejected')),
    ], string='Status', index=True, readonly=True, copy=False, default='draft',
        help="""* When the payslip is created the status is \'Draft\'
                \n* If the payslip is under verification, the status is \'Waiting\'.
                \n* If the payslip is confirmed then status is set to \'Done\'.
                \n* When user cancel payslip the status is \'Rejected\'.""", track_visibility='onchange')

    currency_id = fields.Many2one('res.currency', _('Currency'), default=_get_default_currency_id, required=True)

    payment_ids = fields.One2many('account.payment', 'payslip_id', _('Payments'))

    total_amount = fields.Monetary(string=_('Total Amount'), compute='_compute_total_amount', store=True)

    reconciled = fields.Boolean(string=_('Paid/Reconciled'), store=True, readonly=True, compute='_compute_residual',
                                help="It indicates that the payslip has been paid and the journal entry of the payslip has been reconciled with one or several journal entries of payment.")

    residual = fields.Monetary(string=_('Amount Due'),
                               compute='_compute_residual', store=True, help="Remaining amount due.")

    residual_signed = fields.Monetary(string=_('Amount Due in Other Currency'),
                                      compute='_compute_residual', store=True,
                                      currency_field='currency_id',
                                      help="Remaining amount due in the currency of the invoice.")

    residual_company_signed = fields.Monetary(string=_('Amount Due in Company Currency'),
                                              compute='_compute_residual', store=True,
                                              currency_field='currency_id',
                                              help="Remaining amount due in the currency of the company.")

    @api.depends('line_ids')
    @api.onchange('line_ids')
    def _compute_total_amount(self):

        precision = self.env['decimal.precision'].precision_get('Payroll')

        for slip in self:
            total_amount_new = 0.0
            for line in slip.line_ids:
                amount = slip.credit_note and -line.total or line.total
                if float_is_zero(amount, precision_digits=precision):
                    continue
                total_amount_new += amount
            slip.total_amount = total_amount_new

    def _update_residual(self, move_lines):
        residual = 0.0
        residual_company_signed = 0.0

        for line in move_lines:
            if line.account_id.internal_type in ('receivable', 'payable'):
                residual_company_signed += line.amount_residual
                if line.currency_id == self.currency_id:
                    residual += line.amount_residual_currency if line.currency_id else line.amount_residual
                else:
                    from_currency = (line.currency_id and line.currency_id.with_context(
                        date=line.date)) or line.company_id.currency_id.with_context(date=line.date)
                    residual += from_currency.compute(line.amount_residual_currency, self.currency_id)

        return residual, residual_company_signed

    @api.one
    @api.depends(
        'state', 'line_ids',
        'payment_ids',
        'payment_ids.state',
        'move_id.line_ids.amount_residual',
        'move_id.line_ids.currency_id')
    def _compute_residual(self):
        if self.state not in ['done', 'paid']:
            return

        move_lines = self.sudo().move_id.line_ids
        if not move_lines:
            return

        residual, residual_company_signed = self._update_residual(move_lines)

        payment_residual = 0.0
        payment_residual_company_signed = 0.0
        for payment in self.sudo().payment_ids:
            if payment.move_line_ids:
                payment_residual, payment_residual_company_signed = self._update_residual(payment.move_line_ids)
                residual = residual + payment_residual
                residual_company_signed = residual_company_signed + payment_residual_company_signed

        sign = self.credit_note and -1 or 1
        self.residual_company_signed = abs(residual_company_signed) * sign
        self.residual_signed = abs(residual) * sign
        self.residual = abs(residual)

        digits_rounding_precision = self.currency_id.rounding
        if float_is_zero(self.residual, precision_rounding=digits_rounding_precision):
            self.reconciled = True
        else:
            self.reconciled = False

    @api.multi
    def set_to_paid(self):
        self.write({'state': 'paid'})


class HrPayslipRun(models.Model):
    _inherit = 'hr.payslip.run'

    state = fields.Selection([
        ('draft', _('Draft')),
        ('done', _('Done')),
        ('paid', _('Paid')),
        ('close', _('Close')),
    ], string=_('Status'), index=True, readonly=True, copy=False, default='draft')
    total_amount = fields.Float(string=_('Total Amount'), compute='_compute_total_amount')

    @api.multi
    def batch_wise_payslip_confirm(self):
        for record in self.slip_ids:
            if record.state == 'draft':
                record.action_payslip_done()
        self.state = 'done'


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    payslip_id = fields.Many2one('hr.payslip', string=_('Expense'), copy=False,
                                 help='Expense where the move line come from')

    @api.multi
    def reconcile(self, writeoff_acc_id=False, writeoff_journal_id=False):
        res = super(AccountMoveLine, self).reconcile(writeoff_acc_id=writeoff_acc_id,
                                                     writeoff_journal_id=writeoff_journal_id)
        account_move_ids = []
        for l in self:
            precision_currency = l.move_id.currency_id or l.move_id.company_id.currency_id
            if float_compare(l.move_id.matched_percentage, 1, precision_rounding=precision_currency.rounding) == 0:
                account_move_ids.append(l.move_id.id)

        if account_move_ids:
            payslip = self.env['hr.payslip'].search([
                ('move_id', 'in', account_move_ids), ('state', '=', 'done')
            ])
            payslip.set_to_paid()
        return res


class AccountPayment(models.Model):
    _inherit = "account.payment"

    payslip_id = fields.Many2one('hr.payslip', string=_('Payslip'), copy=False,
                                 help='Payslip where the payment come from')
