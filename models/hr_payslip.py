# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models, _

from odoo.tools import float_is_zero
from odoo.exceptions import ValidationError
from werkzeug import url_encode

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

    @api.multi
    def refund_sheet(self):
        for payslip in self:
            if any(inv.state != 'draft' for inv in payslip.payment_ids):
                raise ValidationError(_("The payslip cannot be refunded, because it has confirmed payments!"))

            if payslip.move_id:
                payslip.move_id.line_ids.remove_move_reconcile()
                payslip.move_id.button_cancel()
                payslip.move_id.unlink()

            copied_payslip = payslip
            copied_payslip.set_to_draft()

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
                    residual += line.amount_residual

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

        for payment in self.sudo().payment_ids:
            if payment.move_line_ids:
                payment_residual, payment_residual_company_signed = self._update_residual(payment.move_line_ids)
                residual = residual + payment_residual
                residual_company_signed = residual_company_signed + payment_residual_company_signed

        sign = self.credit_note and -1 or 1
        self.residual_company_signed = abs(residual_company_signed) * sign
        self.residual_signed = abs(residual) * sign
        self.residual = abs(residual)

        precision = self.env['decimal.precision'].precision_get('Account')
        if 0.05 > self.residual >= 0:
            self.reconciled = True
        else:
            self.reconciled = False

    @api.multi
    def set_to_paid(self):
        if self.state not in 'paid':
            self.write({'state': 'paid'})

    @api.multi
    def set_to_draft(self):
        if self.state not in 'draft':
            self.write({'state': 'draft'})

    @api.model
    def get_contract(self, employee, date_from, date_to):
        if self.contract_id:
            return [self.contract_id.id]
        contract_ids = super(HrPayslip, self).get_contract(employee, date_from, date_to)
        if not contract_ids or not len(contract_ids):
            return []
        return [contract_ids[0]]

    @api.onchange('contract_id')
    def _onchange_contract_id(self):
        self.onchange_employee()


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

    payslip_id = fields.Many2one('hr.payslip', string=_('Payslip'), copy=False,
                                 help='Payslip where the move line come from')

    @api.multi
    def reconcile(self, writeoff_acc_id=False, writeoff_journal_id=False):
        res = super(AccountMoveLine, self).reconcile(writeoff_acc_id=writeoff_acc_id,
                                                     writeoff_journal_id=writeoff_journal_id)
        for rec in self:
            if rec.payment_id.payslip_id and rec.payment_id.payslip_id.reconciled:
                rec.payment_id.payslip_id.set_to_paid()
        return res


class AccountPayment(models.Model):
    _inherit = "account.payment"

    payslip_id = fields.Many2one('hr.payslip', string=_('Payslip'), copy=False,
                                 help='Payslip where the payment come from')

    def _compute_destination_account_id(self):
        destination_account_id = self._context.get('destination_account_id', False)
        if destination_account_id:
            self.destination_account_id = destination_account_id
            return
        super(AccountPayment, self)._compute_destination_account_id()

    @api.multi
    def button_payslips(self):
        return {
            'name': _('Paid Invoices'),
            'view_type': 'form',
            'view_mode': 'tree,form',
            'res_model': 'hr.payslip',
            'view_id': False,
            'type': 'ir.actions.act_window',
            'domain': [('id', 'in', [self.payslip_id.id])],
        }

    @api.multi
    def cancel(self):
        super(AccountPayment, self).cancel()
        for rec in self:
            if rec.payslip_id and rec.payslip_id.state == 'paid':
                rec.payslip_id.state = 'done'

    @api.multi
    def post(self):
        self.ensure_one()

        destination_account_id = None
        payment = None
        payslip = None

        if 'destination_account_id' not in self.env.context:
            for rec in self:
                if rec.payslip_id and rec.payslip_id.state != 'done':
                    raise ValidationError(_("The payment cannot be processed because the payslip is not confirmed!"))
                payslip = rec.payslip_id
                payment = rec
                destination_account_id = payslip.employee_id.address_home_id.property_account_payable_id
                if payslip.move_id:
                    for line in payslip.move_id.line_ids:
                        if line.credit:
                            destination_account_id = line.account_id

        if destination_account_id:
            context = dict(self.env.context)
            context['destination_account_id'] = destination_account_id
            super(AccountPayment, self).with_context(context).post()
        else:
            super(AccountPayment, self).post()

        if payment and payslip:
            body = (_(
                "A payment of %s %s with the reference <a href='/mail/view?%s'>%s</a> related to your expense %s has been made.") % (
                        payment.amount, payment.currency_id.symbol,
                        url_encode({'model': 'account.payment', 'res_id': payment.id}), payment.name, payslip.name))
            payslip.message_post(body=body)

            if payslip.reconciled:
                # Reconcile the payment, i.e. lookup on the payable account move lines
                account_move_lines_to_reconcile = self.env['account.move.line']

                for line in payment.move_line_ids + payslip.move_id.line_ids:
                    if line.account_id.internal_type == 'payable':
                        account_move_lines_to_reconcile |= line
                account_move_lines_to_reconcile.reconcile()

            if payslip.payslip_run_id:
                payslip_paid_search = self.env['hr.payslip'].search(
                    [('payslip_run_id', '=', payslip.payslip_run_id.id), ('state', '=', 'paid')])
                if payslip_paid_search:
                    if len(payslip.payslip_run_id.slip_ids) == len(payslip_paid_search):
                        payslip.payslip_run_id.write({'state': 'paid'})
