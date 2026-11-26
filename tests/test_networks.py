from src.n9n8.networks import NETWORKS, execution_enabled, get_network


def test_network_selector_lists_all_four_requested_networks():
    assert [network.code for network in NETWORKS] == [
        "solana", "base", "ethereum", "bnb"
    ]


def test_only_solana_is_execution_enabled():
    assert execution_enabled("solana") is True
    assert execution_enabled("base") is False
    assert execution_enabled("ethereum") is False
    assert execution_enabled("bnb") is False


def test_network_lookup_is_case_insensitive_and_rejects_unknown_networks():
    assert get_network("BASE").name == "Base"
    assert get_network("unknown") is None
