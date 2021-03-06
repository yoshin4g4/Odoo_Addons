import json
from odoo import models, fields, api, _
from datetime import datetime
from dateutil import parser
import pytz
from .. import shopify

utc = pytz.utc
import time
import logging

_logger = logging.getLogger("shopify_order_process===(Emipro):")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    @api.depends('risk_ids')
    def _check_risk_order(self):
        for order in self:
            flag = False
            for risk in order.risk_ids:
                if risk.recommendation != 'accept':
                    flag = True
                    break
            order.is_risky_order = flag

    def _get_shopify_order_status(self):
        """
        get shopify order and set True if updated_in_shopify
        :return:
        """
        for order in self.filtered(lambda x:x.shopify_order_id != False):
            pickings = order.picking_ids.filtered(lambda x:x.state != "cancel")
            if pickings:
                outgoing_picking = pickings.filtered(
                        lambda x:x.location_dest_id.usage == "customer")
                if all(outgoing_picking.mapped("updated_in_shopify")):
                    order.updated_in_shopify = True
                else:
                    order.updated_in_shopify = False
            else:
                order.updated_in_shopify = False

    def _search_shopify_order_ids(self, operator, value):
        query = """
                    select so.id from stock_picking sp
                    inner join sale_order so on so.procurement_group_id=sp.group_id                   
                    inner join stock_location on stock_location.id=sp.location_dest_id and stock_location.usage='customer'
                    where sp.updated_in_shopify = %s and sp.state != 'cancel' and
                    so.shopify_instance_id notnull
                """ % (value)
        self._cr.execute(query)
        results = self._cr.fetchall()
        order_ids = []
        for result_tuple in results:
            order_ids.append(result_tuple[0])
        order_ids = list(set(order_ids))
        return [('id', 'in', order_ids)]

    shopify_order_id = fields.Char("Shopify Order Ref")
    shopify_order_number = fields.Char("Shopify Order Number")
    shopify_instance_id = fields.Many2one("shopify.instance.ept", "Instance")
    shopify_order_status = fields.Char("Shopify Order Status",
                                       help="Shopify order status when order imported in odoo at the moment order status in Shopify.")
    auto_workflow_process_id = fields.Many2one("sale.workflow.process.ept", "Auto Workflow")
    shopify_payment_gateway_id = fields.Many2one('shopify.payment.gateway.ept',
                                                 string="Payment Gateway")
    risk_ids = fields.One2many("shopify.order.risk", 'odoo_order_id', "Risks")
    shopify_location_id = fields.Many2one("shopify.location.ept", "Shopify Location")
    checkout_id = fields.Char("Checkout Id")
    is_risky_order = fields.Boolean("Risky Order ?", compute=_check_risk_order, store=True)
    updated_in_shopify = fields.Boolean("Updated In Shopify ?", compute=_get_shopify_order_status,
                                        search='_search_shopify_order_ids')
    closed_at_ept = fields.Datetime("Closed At")
    canceled_in_shopify = fields.Boolean("Canceled In Shopify", default=False)

    def import_shopify_orders(self, order_data_queue_line, log_book_id):
        """This method used to create a sale orders in Odoo.
            @param : self, order_data_queue_line
            @return:
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 11/11/2019.
            Task Id : 157350
        """
        comman_log_line_obj = self.env["common.log.lines.ept"]
        res_partner_obj = self.env["res.partner"]
        shopify_location_obj = self.env["shopify.location.ept"]
        instance = order_data_queue_line.shopify_instance_id
        order_data = order_data_queue_line.order_data
        order_response = json.loads(order_data)
        order_response = order_response.get('order')
        model = "sale.order"
        model_id = comman_log_line_obj.get_model_id(model)
        instance.connect_in_shopify()
        _logger.info('Start process of shopify order(%s) and order id is(%s) '
                     % (order_response.get('order_number'), order_response.get('id')))
        order_id = False
        order_id = self.search([('shopify_order_id', '=', order_response.get('id')),
                                ('shopify_instance_id', '=', instance.id),
                                ('shopify_order_number', '=', order_response.get('order_number'))])
        if order_id:
            order_data_queue_line.write({'state': 'done', 'processed_at': datetime.now(),
                                         'sale_order_id': order_id.id})
            _logger.info('Done the Process of order Because Shopify Order(%s) is exist in Odoo and '
                         'Odoo order is(%s)' % (order_response.get('order_number'), order_id.name))
            self._cr.commit()
            return order_id
        # add by bhavesh jadav 05/12/2019
        partner = order_response.get('customer', {}) and res_partner_obj.create_or_update_customer(vals=order_response,
                                                                                                   log_book_id=log_book_id,
                                                                                                   is_company=True,
                                                                                                   parent_id=False,
                                                                                                   type=False,
                                                                                                   instance=instance,
                                                                                                   order_data_queue_line=order_data_queue_line) or False
        if partner and partner.parent_id:
            parent_id = partner.parent_id
        if partner and not partner.parent_id:
            parent_id = partner
        if not partner:
            message = "Customer Not Available In %s Order" % (order_response.get('order_number'))
            comman_log_line_obj.shopify_create_order_log_line(message, model_id,
                                                              order_data_queue_line, log_book_id)
            order_data_queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
            _logger.info('Customer not available in Shopify Order(%s)' % (
                order_response.get('order_number')))
            return False
        # add by bhavesh jadav 05/12/2019
        shipping_address = order_response.get('shipping_address', False) and res_partner_obj.create_or_update_customer(
            vals=order_response.get('shipping_address'), log_book_id=log_book_id, is_company=False,
            parent_id=parent_id.id, type='delivery', instance=instance,
            email=order_response.get('customer', {}).get('email')) or partner
        # add by bhavesh jadav 05/12/2019
        invoice_address = order_response.get('billing_address', False) and res_partner_obj.create_or_update_customer(
            vals=order_response.get('billing_address'), log_book_id=log_book_id, is_company=False,
            parent_id=parent_id.id, type='invoice', instance=instance,
            email=order_response.get('customer', {}).get('email')) or partner
        lines = order_response.get('line_items')
        if self.check_mismatch_details(lines, instance, order_response.get('order_number'),
                                       order_data_queue_line, log_book_id):
            _logger.info('Mis-mismatch details found in this Shopify Order(%s) and id (%s)' % (
                order_response.get('order_number'), order_response.get('id')))
            order_data_queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
            return False
        shopify_location_id = order_response.get('location_id') or False
        if not shopify_location_id:
            shopify_location = shopify_location_obj.search(
                [('is_primary_location', '=', True), ('instance_id', '=', instance.id)],
                limit=1)
        else:
            shopify_location = shopify_location_obj.search(
                [('shopify_location_id', '=', shopify_location_id),
                 ('instance_id', '=', instance.id)],
                limit=1)
        order_id = self.shopify_create_order(instance, partner, shipping_address, invoice_address,
                                             order_data_queue_line, order_response, log_book_id)
        if not order_id:
            _logger.info('Configuration missing in Odoo while importing Shopify Order(%s) '
                         'and id (%s)' % (
                             order_response.get('order_number'), order_response.get('id')))
            order_data_queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
            return False

        order_id.write({'shopify_location_id': shopify_location and shopify_location.id or
                                               False})

        risk_result = shopify.OrderRisk().find(order_id=order_response.get('id'))
        if risk_result:
            self.env["shopify.order.risk"].shopify_create_risk_in_order(risk_result, order_id)
        _logger.info('Creating order line for Odoo order(%s) and Shopify order is (%s)' % (
            order_id.name, order_response.get('order_number')))
        total_discount = order_response.get('total_discounts', 0.0)
        for line in lines:
            shopify_product = self.search_shopify_product_for_order_line(
                line, instance)
            product = shopify_product.product_id
            order_line = self.shopify_create_sale_order_line(line, product, line.get('quantity'),
                                                             product.name, order_id,
                                                             line.get('price'), order_response)
            # add by Bhavesh Jadav 04/12/2019 for create separate  discount line fpr apply tax in discount line
            if float(total_discount) > 0.0:
                discount_amount = 0.0
                for discount_allocation in line.get('discount_allocations'):
                    discount_amount += float(discount_allocation.get('amount'))
                if discount_amount > 0.0:
                    _logger.info('Creating discount line for Odoo order(%s) and Shopify order is (%s)'
                                 % (order_id.name, order_response.get('order_number')))
                    self.shopify_create_sale_order_line({}, instance.discount_product_id, 1,
                                                        product.name, order_id,
                                                        float(discount_amount) * -1, order_response,
                                                        previous_line=order_line, is_discount=True)
                    _logger.info('Created discount line for Odoo order(%s) and Shopify order is (%s)'
                                 % (order_id.name, order_response.get('order_number')))

        _logger.info('Created order line for Odoo order(%s) and Shopify order is (%s)' % (
            order_id.name, order_response.get('order_number')))

        for line in order_response.get('shipping_lines', []):
            carrier = self.env["delivery.carrier"].shopify_search_create_delivery_carrier(line)
            order_id.write({'carrier_id': carrier.id if carrier else False})
            shipping_product = carrier.product_id
            self.shopify_create_sale_order_line(line, carrier.product_id, 1,
                                                carrier.product_id.name or line.get('title'),
                                                order_id, line.get('price'), order_response,
                                                is_shipping=True)
        _logger.info('Start auto workflow process for Odoo order(%s) and Shopify order is (%s)'
                     % (order_id.name, order_response.get('order_number')))
        self.env['sale.workflow.process.ept'].auto_workflow_process(ids=order_id.ids)
        _logger.info('Done auto workflow process for Odoo order(%s) and Shopify order is (%s)'
                     % (order_id.name, order_response.get('order_number')))
        order_data_queue_line.write({'state': 'done', 'processed_at': datetime.now(),
                                     'sale_order_id': order_id.id})
        _logger.info('Done the Odoo Order(%s) create process and Shopify Order (%s)' % (
            order_id.name, order_response.get('order_number')))
        return order_id

    def check_mismatch_details(self, lines, instance, order_number, order_data_queue_line, log_book_id):
        """This method used to check the mismatch details in the order lines.
            @param : self, lines, instance, order_number, order_data_queue_line
            @return:
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 11/11/2019.
            Task Id : 157350
        """
        odoo_product_obj = self.env['product.product']
        shopify_product_obj = self.env['shopify.product.product.ept']
        shopify_product_template_obj = self.env['shopify.product.template.ept']
        comman_log_line_obj = self.env["common.log.lines.ept"]
        model = "sale.order"
        model_id = comman_log_line_obj.get_model_id(model)
        mismatch = False
        for line in lines:
            shopify_variant = False
            sku = line.get('sku') or False
            if line.get('variant_id', None):
                shopify_variant = shopify_product_obj.search(
                    [('variant_id', '=', line.get('variant_id')),
                     ('shopify_instance_id', '=', instance.id)])
            if not shopify_variant and sku:
                shopify_variant = shopify_product_obj.search(
                    [('default_code', '=', sku),
                     ('shopify_instance_id', '=', instance.id)])
            if shopify_variant:
                continue
            if not shopify_variant:
                line_variant_id = line.get('variant_id', False)
                line_product_id = line.get('product_id', False)
                if line_product_id and line_variant_id:
                    shopify_product_template_obj.shopify_sync_products(False, line_product_id,
                                                                       instance, log_book_id, order_data_queue_line)
                    if line.get('variant_id', None):
                        shopify_variant = shopify_product_obj.search(
                            [('variant_id', '=', line.get('variant_id')),
                             ('shopify_instance_id', '=', instance.id)])
                    if not shopify_variant and sku:
                        shopify_variant = shopify_product_obj.search(
                            [('default_code', '=', sku),
                             ('shopify_instance_id', '=', instance.id)])
                    if not shopify_variant:
                        mismatch = True
                        break
                else:
                    message = "Product Id Not Available In %s Order Line response" % (order_number)
                    comman_log_line_obj.shopify_create_order_log_line(message, model_id,
                                                                      order_data_queue_line, log_book_id)
                    order_data_queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
                    mismatch = True
                    break
        return mismatch

    def shopify_create_order(self, instance, partner, shipping_address, invoice_address,
                             order_data_queue_line, order_response, log_book_id):
        """This method used to create a sale order.
            @param : self, instance, partner, shipping_address, invoice_address,order_data_queue_line, order_response
            @return: order
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 12/11/2019.
            Task Id : 157350
        """
        payment_gateway, workflow = self.env[
            "shopify.payment.gateway.ept"].shopify_search_create_gateway_workflow(instance,
                                                                                  order_data_queue_line,
                                                                                  order_response, log_book_id)

        if not all([payment_gateway, workflow]):
            return False

        order_vals = self.prepare_shopify_order_vals(instance, partner, shipping_address,
                                                     invoice_address, order_response,
                                                     payment_gateway,
                                                     workflow)

        order = self.create(order_vals)
        return order

    def prepare_shopify_order_vals(self, instance, partner, shipping_address,
                                   invoice_address, order_response, payment_gateway,
                                   workflow):
        """This method used to Prepare a order vals.
            @param : self, instance, partner, shipping_address,invoice_address, order_response, payment_gateway,workflow
            @return: order_vals
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13/11/2019.
            Task Id : 157350
        """
        if order_response.get('created_at', False):
            order_date = order_response.get('created_at', False)
            date_order = parser.parse(order_date).astimezone(utc).strftime('%Y-%m-%d %H:%M:%S')
        else:
            date_order = time.strftime('%Y-%m-%d %H:%M:%S')
            date_order = str(date_order)
        # add by Bhavesh Jadav 09/12/2019 for set price list based on the order response currency
        pricelist_id = self.shopify_set_pricelist(order_response=order_response,instance=instance)
        ordervals = {
            'company_id': instance.shopify_company_id.id if instance.shopify_company_id else False,
            'partner_id': partner.ids[0],
            'partner_invoice_id': invoice_address.ids[0],
            'partner_shipping_id': shipping_address.ids[0],
            'warehouse_id': instance.shopify_warehouse_id.id if instance.shopify_warehouse_id else False,
            'date_order': date_order,
            'state': 'draft',
            'pricelist_id': pricelist_id.id if pricelist_id else False,
            'team_id': instance.shopify_section_id.id if instance.shopify_section_id else False,
        }
        ordervals = self.create_sales_order_vals_ept(ordervals)
        # add by bhavesh jadav because 30/11/2019 in common connector  new_record.onchange_partner_id() return wrong invoice and shipping address so i update vals again with the right address
        ordervals.update({'partner_id': partner and partner.ids[0],
                          'partner_invoice_id': invoice_address and invoice_address.ids[0],
                          'partner_shipping_id': shipping_address and shipping_address.ids[0]})

        ordervals.update({
            'checkout_id': order_response.get('checkout_id'),
            'note': order_response.get('note'),
            'shopify_order_id': order_response.get('id'),
            'shopify_order_number': order_response.get('order_number'),
            'shopify_payment_gateway_id': payment_gateway and payment_gateway.id or False,
            'shopify_instance_id': instance.id,
            'global_channel_id':instance.shopify_global_channel_id and instance.shopify_global_channel_id.id or False,
            'shopify_order_status': order_response.get('fulfillment_status'),
            'picking_policy': workflow.picking_policy or False,
            'auto_workflow_process_id': workflow and workflow.id,
            # 'payment_term_id':payment_term_id and payment_term_id or payment_term or False,
            #'invoice_policy': workflow.invoice_policy or False
        })
        if not instance.is_use_default_sequence:
            if instance.shopify_order_prefix:
                name = "%s_%s" % (instance.shopify_order_prefix, order_response.get('name'))
            else:
                name = order_response.get('name')
            ordervals.update({'name': name})
        return ordervals

    def shopify_set_pricelist(self, instance, order_response):
        """
        Author:Bhavesh Jadav 09/12/2019 for the for set price list based on the order response currency because of if
        order currency different then the erp currency so we need to set proper pricelist for that sale order
        otherwise set pricelist  based on instance configurations
        """
        currency_obj = self.env['res.currency']
        pricelist_obj = self.env['product.pricelist']
        order_currency = order_response.get('currency') or False
        if order_currency:
            currency = currency_obj.search([('name', '=', order_currency)])
            if not currency:
                currency = currency_obj.search([('name', '=', order_currency), ('active', '=', False)])
                if currency:
                    currency.write({'active': True})
                    pricelist = pricelist_obj.search([('currency_id', '=', currency.id)], limit=1)
                    if pricelist:
                        return pricelist
                    else:
                        pricelist_vals = {'name': currency.name,
                                          'currency_id': currency.id,
                                          'company_id': instance.shopify_company_id.id, }
                        pricelist = pricelist_obj.create(pricelist_vals)
                        return pricelist
                else:
                    pricelist = instance.shopify_pricelist_id.id if instance.shopify_pricelist_id else False
                    return pricelist
            else:
                pricelist = pricelist_obj.search([('currency_id', '=', currency.id)], limit=1)
                return pricelist
        else:
            pricelist = instance.shopify_pricelist_id.id if instance.shopify_pricelist_id else False
            return pricelist

    def search_shopify_product_for_order_line(self, line, instance):
        """This method used to search shopify product for order line.
            @param : self, line, instance
            @return: shopify_product
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 14/11/2019.
            Task Id : 157350
        """
        shopify_product_obj = self.env['shopify.product.product.ept']
        variant_id = line.get('variant_id')
        shopify_product = False
        shopify_product = shopify_product_obj.search(
            [('shopify_instance_id', '=', instance.id), ('variant_id', '=', variant_id)])
        if shopify_product:
            return shopify_product
        shopify_product = shopify_product_obj.search([('shopify_instance_id', '=', instance.id),
                                                      ('default_code', '=', line.get('sku'))])
        shopify_product and shopify_product.write({'variant_id': variant_id})
        if shopify_product:
            return shopify_product

    def shopify_create_sale_order_line(self, line, product, quantity,
                                       product_name, order_id,
                                       price, order_response, is_shipping=False, previous_line=False,
                                       is_discount=False):
        """This method used to create a sale order line.
            @param : self, line, product, quantity,product_name, order_id,price, is_shipping=False
            @return: order_line_id
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 14/11/2019.
            Task Id : 157350
        """
        sale_order_line_obj = self.env['sale.order.line']

        uom_id = product and product.uom_id and product.uom_id.id or False
        line_vals = {
            'product_id': product and product.ids[0] or False,
            'order_id': order_id.id,
            'company_id': order_id.company_id.id,
            'product_uom': uom_id,
            'name': product_name,
            'price_unit': price,
            'order_qty': quantity,
        }
        order_line_vals = sale_order_line_obj.create_sale_order_line_ept(line_vals)
        if order_id.shopify_instance_id.apply_tax_in_order == 'create_shopify_tax':
            taxes_included = order_response.get('taxes_included') or False
            tax_ids = []
            if line and line.get('tax_lines'):
                if line.get('taxable'):
                #This is used for when the one product is taxable and another product is not
                # taxable
                    tax_ids = self.shopify_get_tax_id_ept(order_id.shopify_instance_id, line.get('tax_lines'),
                                                          taxes_included)
                if is_shipping:
                    # In the Shopify store there is configuration regarding tax is applicable on shipping or not, if applicable then this use.
                    tax_ids = self.shopify_get_tax_id_ept(order_id.shopify_instance_id,
                                                          line.get('tax_lines'),
                                                          taxes_included)
            elif not line:
                tax_ids = self.shopify_get_tax_id_ept(order_id.shopify_instance_id,
                                                      order_response.get('tax_lines'),
                                                      taxes_included)
            order_line_vals["tax_id"] = tax_ids
            # When the one order with two products one product with tax and another product
            # without tax and apply the discount on order that time not apply tax on discount
            # which is
            if is_discount and not previous_line.tax_id:
                order_line_vals["tax_id"] = []
        else:
            if is_shipping and not line.get("tax_lines", []):
                order_line_vals["tax_id"] = []

        if is_discount:
            order_line_vals["name"]='Discount for '+str(product_name)
            if order_id.shopify_instance_id.apply_tax_in_order == 'odoo_tax' and is_discount:  # add by bhavesh jadav 04/12/2019 for by pass odoo tax flow on discount line
                order_line_vals["tax_id"] = previous_line.tax_id

        order_line_vals.update({
            'shopify_line_id': line.get('id'),
            'is_delivery': is_shipping,
        })
        order_line_id = sale_order_line_obj.create(order_line_vals)
        return order_line_id

    @api.model
    def shopify_get_tax_id_ept(self, instance, tax_lines, tax_included):
        """This method used to search tax in Odoo.
            @param : self,instance,order_line,tax_included
            @return: tax_id
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 18/11/2019.
            Task Id : 157350
        """
        tax_id = []
        taxes = []
        company=instance.shopify_warehouse_id.company_id
        for tax in tax_lines:
            rate = float(tax.get('rate', 0.0))
            title=tax.get('title')
            rate = rate * 100
            if rate != 0.0:
                # add condition by Bhavesh Jadav 19/12/2019 for if the rate is same other details is same but name
                # its different then its apply wrong tax
                if tax_included:
                    name = '%s_(%s %s included)_%s' % (title, str(rate), '%', company.name)
                else:
                    name = '%s_(%s %s excluded)_%s' % (title, str(rate), '%', company.name)
                acctax_id = self.env['account.tax'].search(
                    [('price_include', '=', tax_included), ('type_tax_use', '=', 'sale'),
                     ('amount', '=', rate),('name','=',name),
                     ('company_id', '=', instance.shopify_warehouse_id.company_id.id)], limit=1)
                if not acctax_id:
                    acctax_id = self.shopify_create_account_tax(instance, rate, tax_included,
                                                                company,name)
                if acctax_id:
                    taxes.append(acctax_id.id)
        if taxes:
            tax_id = [(6, 0, taxes)]
        return tax_id

    @api.model
    def shopify_create_account_tax(self, instance, value, price_included, company,name):
        """This method used to create tax in Odoo when importing orders from Shopify to Odoo.
            @param : self, value, price_included, company, name
            @return: account_tax_id
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 18/11/2019.
            Task Id : 157350
        """
        account_tax_obj = self.env['account.tax']
        account_tax_id = account_tax_obj.create(
            {'name': name, 'amount': float(value), 'type_tax_use': 'sale',
             'price_include': price_included, 'company_id': company.id})
        account_tax_id.mapped('invoice_repartition_line_ids').write(
            {'account_id': instance.invoice_tax_account_id.id if
            instance.invoice_tax_account_id else False})
        account_tax_id.mapped('refund_repartition_line_ids').write(
            {'account_id': instance.credit_tax_account_id.id if instance.credit_tax_account_id
            else False})

        return account_tax_id

    @api.model
    def closed_at(self, instance):
        sales_orders = self.search([('warehouse_id', '=', instance.shopify_warehouse_id.id),
                                    ('shopify_order_id', '!=', False),
                                    ('shopify_instance_id', '=', instance.id),
                                    ('state', '=', 'done'), ('closed_at_ept', '=', False)],
                                   order='date_order')

        instance.connect_in_shopify()

        for sale_order in sales_orders:
            order = shopify.Order.find(sale_order.shopify_order_id)
            order.close()
            sale_order.write({'closed_at_ept': datetime.now()})
        return True

    def update_order_status_in_shopify(self, instance):
        """
        find the picking with below condition
            1. shopify_instance_id = instance.id
            2. updated_in_shopify = False
            3. state = Done
            4. location_dest_id.usage = customer
        get order line data from the picking and process on that. Process on only those products which type is not service.
        get carrier_name from the picking
        get product qty from move lines. If one move having multiple move lines then total qty of all the move lines.
        shopify_line_id wise set the product qty_done
        set tracking details
        using shopify Fulfillment API update the order status
        :param instance:
        :return:
        @author: Angel Patel @Emipro Technologies Pvt. Ltd.
        Task Id : 157905
        """
        log_line_array = []
        comman_log_line_obj = self.env["common.log.lines.ept"]
        shopify_product_obj = self.env['shopify.product.product.ept']
        model = "sale.order"
        model_id = comman_log_line_obj.get_model_id(model)
        _logger.info(_("Update Order Status process start and you select '%s' Instance") % instance.name)
        stock_picking_obj = self.env['stock.picking']
        move_line_obj = self.env['stock.move.line']
        instance.connect_in_shopify()
        notify_customer = instance.notify_customer
        picking_ids = stock_picking_obj.search(
            [('shopify_instance_id', '=', instance.id), ('updated_in_shopify', '=', False), ('state', '=', 'done'),
             ('location_dest_id.usage', '=', 'customer')],
            order='date')

        for picking in picking_ids:
            _logger.info(("We are processing on picking ID : '%s'" % picking.id))
            sale_order = picking.sale_id
            _logger.info(("We are processing on sale order ID : '%s'" % sale_order.id))
            order_lines = sale_order.order_line
            if order_lines and order_lines.filtered(
                    lambda s: s.product_id.type != 'service' and s.shopify_line_id == False or ''):
                message = (_(
                    "Order status is not updated for order %s because shopify line id not found in this order." % picking.sale_id.name))
                _logger.info(message)
                log_line_array = shopify_product_obj.shopify_create_log(message, model_id, False, log_line_array)
                continue

            line_items = {}
            list_of_tracking_number = []
            tracking_numbers = []
            carrier_name = picking.carrier_id and picking.carrier_id.shopify_code or ''
            if not carrier_name:
                carrier_name = picking.carrier_id and picking.carrier_id.name or ''
            for move in picking.move_lines:
                _logger.info(("We are processing on move line ID : '%s'" % move.id))
                if move.sale_line_id and move.sale_line_id.shopify_line_id:
                    shopify_line_id = move.sale_line_id.shopify_line_id

                """Create Package for the each parcel"""
                # move_line = move_line_obj.search(
                #     [('move_id', '=', move.id), ('product_id', '=', move.product_id.id)],
                #     limit=1)
                stock_move_lines = move_line_obj.search(
                    [('move_id', '=', move.id), ('product_id', '=', move.product_id.id)])
                tracking_no = picking.carrier_tracking_ref or False
                product_qty = 0.0
                for move_line in stock_move_lines:
                    if move_line.result_package_id.tracking_no:
                        tracking_no = move_line.result_package_id.tracking_no
                    if (move_line.package_id and move_line.package_id.tracking_no):
                        tracking_no = move_line.package_id.tracking_no

                    tracking_no and list_of_tracking_number.append(tracking_no)
                    product_qty += move_line.qty_done or 0.0
                product_qty = int(product_qty)

                if shopify_line_id in line_items:
                    if 'tracking_no' in line_items.get(shopify_line_id):
                        quantity = line_items.get(shopify_line_id).get('quantity')
                        quantity = quantity + product_qty
                        line_items.get(shopify_line_id).update({'quantity': quantity})
                        if tracking_no not in line_items.get(shopify_line_id).get(
                                'tracking_no'):
                            line_items.get(shopify_line_id).get('tracking_no').append(
                                tracking_no)
                    else:
                        line_items.get(shopify_line_id).update({'tracking_no': []})
                        line_items.get(shopify_line_id).update({'quantity': product_qty})
                        line_items.get(shopify_line_id).get('tracking_no').append(
                            tracking_no)
                else:
                    line_items.update({shopify_line_id: {}})
                    line_items.get(shopify_line_id).update({'tracking_no': []})
                    line_items.get(shopify_line_id).update({'quantity': product_qty})
                    line_items.get(shopify_line_id).get('tracking_no').append(tracking_no)

            update_lines = []
            for sale_line_id in line_items:
                tracking_numbers += line_items.get(sale_line_id).get('tracking_no')
                update_lines.append({'id': sale_line_id,
                                     'quantity': line_items.get(sale_line_id).get(
                                         'quantity')})

            if not update_lines:
                message = "No lines found for update order status for %s" % (picking.name)
                _logger.info(message)
                log_line_array = shopify_product_obj.shopify_create_log(message, model_id, False, log_line_array)
                continue

            try:
                shopify_location_id = sale_order.shopify_location_id or False
                if not shopify_location_id:
                    location_id = self.env['shopify.location.ept'].search(
                        [('is_primary_location', '=', True),
                         ('instance_id', '=', instance.id)])
                    shopify_location_id = location_id.shopify_location_id or False
                    if not location_id:
                        message = "Primary Location not found for instance %s while Update order status" % (
                            instance.name)
                        _logger.info(message)
                        log_line_array = shopify_product_obj.shopify_create_log(message, model_id, False,
                                                                                log_line_array)
                        continue
                try:
                    new_fulfillment = shopify.Fulfillment(
                        {'order_id': sale_order.shopify_order_id,
                         'location_id': shopify_location_id.shopify_location_id,
                         'tracking_numbers': list(set(tracking_numbers)),
                         'tracking_company': carrier_name, 'line_items': update_lines,
                         'notify_customer': notify_customer})
                except Exception as e:
                    if e.response.code == 429 and e.response.msg == "Too Many Requests":
                        time.sleep(5)
                        new_fulfillment = shopify.Fulfillment(
                            {'order_id': sale_order.shopify_order_id,
                             'location_id': shopify_location_id.shopify_location_id,
                             'tracking_numbers': list(set(tracking_numbers)),
                             'tracking_company': carrier_name, 'line_items': update_lines,
                             'notify_customer': notify_customer})
                fulfillment_result = new_fulfillment.save()
                if not fulfillment_result:
                    message = "Order(%s) status not updated due to some issue in fulfillment request/response:" % (
                        sale_order.name)
                    _logger.info(message)
                    log_line_array = shopify_product_obj.shopify_create_log(message, model_id, False, log_line_array)
                    continue

            except Exception as e:
                message = "%s" % (e)
                _logger.info(message)
                log_line_array = shopify_product_obj.shopify_create_log(message, model_id, False, log_line_array)
                continue

            picking.write({'updated_in_shopify': True})

        if len(log_line_array) > 0:
            shopify_product_obj.create_log(log_line_array, "export", instance)

        self.closed_at(instance)
        return True

    def _prepare_invoice(self):
        """This method used set a shopify instance in customer invoice.
            @param : self
            @return: inv_val
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20/11/2019.
            Task Id : 157911
        """
        inv_val = super(SaleOrder, self)._prepare_invoice()
        if self.shopify_instance_id:
            inv_val.update({'shopify_instance_id': self.shopify_instance_id.id})
        return inv_val

    def cancel_in_shopify(self):
        """This method used to open a wizard to cancel order in Shopify.
            @param : self
            @return: action
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20/11/2019.
            Task Id : 157911
        """
        view = self.env.ref('shopify_ept.view_shopify_cancel_order_wizard')
        context = dict(self._context)
        context.update({'active_model': 'sale.order', 'active_id': self.id, 'active_ids': self.ids})
        return {
            'name': _('Cancel Order In Shopify'),
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'shopify.cancel.refund.order.wizard',
            'views': [(view.id, 'form')],
            'view_id': view.id,
            'target': 'new',
            'context': context
        }


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    shopify_line_id = fields.Char("Shopify Line")


class ImportShopifyOrderStatus(models.Model):
    _name = "import.shopify.order.status"
    _description = 'Order Status'

    name = fields.Char("Name")
    status = fields.Char("Status")
