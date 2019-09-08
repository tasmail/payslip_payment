# -*- coding: utf-8 -*-
import logging
import pytz
import time
import babel

from odoo import _, api, fields, models, tools, _
from odoo.addons.mail.models.mail_template import format_tz
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.translate import html_translate

from datetime import datetime
from datetime import time as datetime_time
from dateutil import relativedelta
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)

class HrPayslip(models.Model):
    _name = 'hr.payslip'
    _inherit = ['hr.payslip', 'mail.thread']

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
    total_amount = fields.Float(string='Total Amount', compute='compute_total_amount', store=True)

    @api.depends('line_ids')
    @api.onchange('line_ids')
    def compute_total_amount(self):

        precision = self.env['decimal.precision'].precision_get('Payroll')

        for slip in self:
            total_amount_new = 0.0
            for line in slip.line_ids:
                amount = slip.credit_note and -line.total or line.total
                if float_is_zero(amount, precision_digits=precision):
                    continue
                total_amount_new += amount
            slip.total_amount = total_amount_new

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
    total_amount = fields.Float(string=_('Total Amount'), compute='compute_total_amount')

    @api.multi
    def batch_wise_payslip_confirm(self):
        for record in self.slip_ids:
            if record.state == 'draft':
                record.action_payslip_done()
        self.state='done'

class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    payslip_id = fields.Many2one('hr.payslip', string=_('Expense'), copy=False, help="Expense where the move line come from")

    @api.multi
    def reconcile(self, writeoff_acc_id=False, writeoff_journal_id=False):
        res = super(AccountMoveLine, self).reconcile(writeoff_acc_id=writeoff_acc_id, writeoff_journal_id=writeoff_journal_id)
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