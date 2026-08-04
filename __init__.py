from . import controllers
from . import models
import json
import uuid
import logging

_logger = logging.getLogger(__name__)

def add_collection_filter(dashboard, filter_def):
    if not dashboard or not dashboard.spreadsheet_data:
        return
    try:
        data = json.loads(dashboard.spreadsheet_data)
    except Exception as e:
        _logger.error("Failed to parse spreadsheet data for %s: %s", dashboard.name, e)
        return
        
    global_filters = data.setdefault('globalFilters', [])
    # Check if a filter for product.collection already exists
    if any(f.get('modelName') == 'product.collection' for f in global_filters):
        _logger.info("Collection filter already exists in %s", dashboard.name)
        return
        
    global_filters.append(filter_def)
    dashboard.spreadsheet_data = json.dumps(data)
    _logger.info("Successfully added Collection filter to %s", dashboard.name)

def post_init_hook(env):
    _logger.info("Running post_init_hook to add Collection filter to Odoo spreadsheet dashboards")
    
    # 1. Update Sales Dashboard
    sales_dashboard = env.ref('spreadsheet_dashboard_sale.spreadsheet_dashboard_sales', raise_if_not_found=False)
    if sales_dashboard:
        sales_filter = {
            "id": str(uuid.uuid4()),
            "type": "relation",
            "label": "Collection",
            "modelName": "product.collection",
            "defaultValue": [],
            "defaultValueDisplayNames": [],
            "rangeType": "year",
            "defaultsToCurrentPeriod": False,
            "pivotFields": {
                "3": {"field": "collection_id", "type": "many2one"},
                "4": {"field": "collection_id", "type": "many2one"},
                "5": {"field": "collection_id", "type": "many2one"},
                "6": {"field": "collection_id", "type": "many2one"},
                "7": {"field": "collection_id", "type": "many2one"},
                "8": {"field": "collection_id", "type": "many2one"},
                "9": {"field": "collection_id", "type": "many2one"},
                "10": {"field": "collection_id", "type": "many2one"},
                "11": {"field": "product_id.collection_id", "type": "many2one"},
                "12": {"field": "product_id.collection_id", "type": "many2one"}
            },
            "listFields": {
                "1": {"field": "order_line.product_id.collection_id", "type": "many2one"},
                "2": {"field": "order_line.product_id.collection_id", "type": "many2one"}
            },
            "graphFields": {
                "a527960b-0812-4291-baba-f6b4b5280a0d": {"field": "collection_id", "type": "many2one"}
            }
        }
        add_collection_filter(sales_dashboard, sales_filter)
    else:
        _logger.warning("Sales dashboard record not found!")

    # 2. Update Product Dashboard
    product_dashboard = env.ref('spreadsheet_dashboard_sale.spreadsheet_dashboard_product', raise_if_not_found=False)
    if product_dashboard:
        product_filter = {
            "id": str(uuid.uuid4()),
            "type": "relation",
            "label": "Collection",
            "modelName": "product.collection",
            "defaultValue": [],
            "defaultValueDisplayNames": [],
            "rangeType": "year",
            "defaultsToCurrentPeriod": False,
            "pivotFields": {
                "1": {"field": "collection_id", "type": "many2one"},
                "2": {"field": "collection_id", "type": "many2one"}
            },
            "listFields": {},
            "graphFields": {
                "e35856cf-9090-489b-b055-2d441380d954": {"field": "collection_id", "type": "many2one"},
                "d0171069-d2cd-4c2c-a686-cd7515e93bb5": {"field": "collection_id", "type": "many2one"}
            }
        }
        add_collection_filter(product_dashboard, product_filter)
    else:
        _logger.warning("Product dashboard record not found!")
