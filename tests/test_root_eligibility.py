"""Decision D1 (2026-09-03): a new transfer chain may only start from an
ERP root at status 13 (Pending). Existing chains keep working regardless."""
from src.jutetransfer.jute_mr_chain_helpers import is_root_eligible_for_new_chain, ROOT_STATUS_LABELS


def test_only_pending_starts_a_chain():
    assert is_root_eligible_for_new_chain(13) is True
    for s in (1, 3, 4, 6, 20, 21, 48, None):
        assert is_root_eligible_for_new_chain(s) is False


def test_labels_cover_erp_vocabulary():
    assert ROOT_STATUS_LABELS[13] == "Pending (Transfer)" and ROOT_STATUS_LABELS[3] == "Approved"
