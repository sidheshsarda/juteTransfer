"""Pages module for JuteTransfer application."""

from .jute_mr import jute_mr_table_page
from .schema_viewer import schema_viewer_page
from .new_transfer_chain import transfer_chain_page
from .company_pl_dashboard import company_pl_dashboard_page

__all__ = [
    'jute_mr_table_page',
    'schema_viewer_page',
    'transfer_chain_page',
    'company_pl_dashboard_page',
]
