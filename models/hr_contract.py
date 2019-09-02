# -*- coding: utf-8 -*-
from odoo import fields, models


class Contract(models.Model):
    _inherit = 'hr.contract'

    wage = fields.Float('Wage', digits=(16, 4), required=True, help="Basic Salary of the employee")
