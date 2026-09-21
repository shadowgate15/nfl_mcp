"""Tests for waiver_tools module."""
from unittest.mock import patch

import pytest

from nfl_mcp.waiver_tools import (
    WaiverAnalyzer,
    _adapt_espn_transactions,
    check_re_entry_status,
    get_waiver_log,
    get_waiver_wire_dashboard,
)


class TestAdaptEspnTransactions:
    """Test the ESPN items[]-to-adds/drops adapter."""

    def test_adapts_add_and_drop_items(self):
        """An ADD item and a DROP item fold into flat adds/drops dicts."""
        transactions = [
            {
                "id": "tx-1",
                "type": "WAIVER",
                "status": "EXECUTED",
                "teamId": 5,
                "scoringPeriodId": 3,
                "processDate": 1735689600000,
                "items": [
                    {"type": "ADD", "playerId": 111, "toTeamId": 5},
                    {"type": "DROP", "playerId": 222, "fromTeamId": 5},
                ],
            }
        ]

        result = _adapt_espn_transactions(transactions)

        assert len(result) == 1
        adapted = result[0]
        assert adapted["transaction_id"] == "tx-1"
        assert adapted["type"] == "WAIVER"
        assert adapted["status"] == "EXECUTED"
        assert adapted["created"] == 1735689600000
        assert adapted["week"] == 3
        assert adapted["adds"] == {"111": 5}
        assert adapted["drops"] == {"222": 5}
        assert adapted["roster_ids"] == [5]

    def test_falls_back_to_proposed_date_when_process_date_absent(self):
        """A pending transaction with no processDate falls back to proposedDate."""
        transactions = [
            {
                "id": "tx-2",
                "type": "FREEAGENT",
                "status": "PENDING",
                "proposedDate": 1736089600000,
                "items": [{"type": "ADD", "playerId": 333, "toTeamId": 7}],
            }
        ]

        result = _adapt_espn_transactions(transactions)

        assert result[0]["created"] == 1736089600000
        assert result[0]["adds"] == {"333": 7}

    def test_ignores_items_with_no_player_id(self):
        """Items missing a playerId are skipped rather than raising."""
        transactions = [
            {
                "id": "tx-3",
                "type": "WAIVER",
                "items": [{"type": "ADD", "toTeamId": 5}],
            }
        ]

        result = _adapt_espn_transactions(transactions)

        assert result[0]["adds"] == {}
        assert result[0]["drops"] == {}

    def test_no_waiver_budget_key_present(self):
        """ESPN has no waiver_budget equivalent, so the adapter never emits one."""
        transactions = [{"id": "tx-4", "type": "WAIVER", "items": []}]

        result = _adapt_espn_transactions(transactions)

        assert "waiver_budget" not in result[0]

    def test_empty_transactions_list(self):
        """An empty input list adapts to an empty output list."""
        assert _adapt_espn_transactions([]) == []


class TestWaiverAnalyzer:
    """Test WaiverAnalyzer class."""

    @pytest.fixture
    def analyzer(self):
        return WaiverAnalyzer()

    def test_extract_waiver_transactions_waiver_type(self, analyzer):
        """Test extraction of waiver transactions with ESPN's WAIVER type."""
        transactions = [
            {
                'type': 'WAIVER',
                'transaction_id': '1',
                'adds': {'player1': 'roster1'},
                'drops': {'player2': 'roster1'},
                'roster_ids': ['roster1'],
                'created': '2026-01-01'
            }
        ]

        result = analyzer._extract_waiver_transactions(transactions)

        assert len(result) == 1
        assert result[0]['type'] == 'WAIVER'
        assert 'player1' in result[0]['adds']

    def test_extract_waiver_transactions_free_agent_type(self, analyzer):
        """Test extraction of free agent transactions with ESPN's FREEAGENT type."""
        transactions = [
            {
                'type': 'FREEAGENT',
                'transaction_id': '2',
                'adds': {'player3': 'roster2'},
                'drops': {},
                'roster_ids': ['roster2'],
                'created': '2026-01-02'
            }
        ]

        result = analyzer._extract_waiver_transactions(transactions)

        assert len(result) == 1
        assert result[0]['type'] == 'FREEAGENT'

    def test_extract_waiver_transactions_non_waiver(self, analyzer):
        """Test that non-waiver transactions are filtered out."""
        transactions = [
            {
                'type': 'TRADE',  # Not a waiver transaction
                'transaction_id': '3',
                'adds': {'player4': 'roster3'},
                'drops': {},
                'roster_ids': ['roster3']
            }
        ]

        result = analyzer._extract_waiver_transactions(transactions)

        assert len(result) == 0

    def test_deduplicate_waiver_log_no_duplicates(self, analyzer):
        """Test deduplication with no duplicates."""
        transactions = [
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'roster_ids': ['roster1'],
                'created': '2026-01-01'
            },
            {
                'adds': {'player2': 'roster1'},
                'drops': {},
                'roster_ids': ['roster1'],
                'created': '2026-01-02'
            }
        ]

        unique, duplicates = analyzer._deduplicate_waiver_log(transactions)

        assert len(unique) == 2
        assert len(duplicates) == 0

    def test_deduplicate_waiver_log_with_duplicates(self, analyzer):
        """Test deduplication with duplicates."""
        transactions = [
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'roster_ids': ['roster1'],
                'created': '2026-01-01'
            },
            {
                'adds': {'player1': 'roster1'},  # Same signature
                'drops': {},
                'roster_ids': ['roster1'],
                'created': '2026-01-01'
            }
        ]

        unique, duplicates = analyzer._deduplicate_waiver_log(transactions)

        assert len(unique) == 1
        assert len(duplicates) == 1

    def test_track_re_entries_simple(self, analyzer):
        """Test re-entry tracking with simple add/drop pattern."""
        transactions = [
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'created': 1735689600  # 2025-01-01 in epoch
            },
            {
                'adds': {},
                'drops': {'player1': 'roster1'},
                'created': 1736089600  # 2025-01-05 in epoch
            }
        ]

        result = analyzer._track_re_entries(transactions)

        # player1 should have activity
        assert 'player1' in result
        assert result['player1']['total_activities'] == 2

    def test_track_re_entries_with_reentry(self, analyzer):
        """Test re-entry tracking with add after drop."""
        transactions = [
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'created': 1735689600  # 2025-01-01 in epoch
            },
            {
                'adds': {},
                'drops': {'player1': 'roster1'},
                'created': 1736089600  # 2025-01-05 in epoch
            },
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'created': 1736489600  # 2025-01-10 in epoch
            }
        ]

        result = analyzer._track_re_entries(transactions)

        assert 'player1' in result
        re_entries = result['player1'].get('re_entries', [])
        assert len(re_entries) > 0


def _make_espn_transactions_response(items_by_transaction):
    """Build a get_espn_transactions-shaped success response from a list of
    (transaction_id, type, adds_items, drops_items) tuples."""
    transactions = []
    for i, (tx_id, tx_type, add_player_team, drop_player_team) in enumerate(items_by_transaction):
        items = []
        for player_id, team_id in add_player_team:
            items.append({"type": "ADD", "playerId": player_id, "toTeamId": team_id})
        for player_id, team_id in drop_player_team:
            items.append({"type": "DROP", "playerId": player_id, "fromTeamId": team_id})
        transactions.append({
            "id": tx_id,
            "type": tx_type,
            "status": "EXECUTED",
            "teamId": add_player_team[0][1] if add_player_team else None,
            "scoringPeriodId": 1,
            "processDate": 1735689600000 + i,
            "items": items,
        })
    return {"success": True, "transactions": transactions, "total_transactions": len(transactions)}


class TestGetWaiverLog:
    """Test get_waiver_log async function."""

    @pytest.mark.asyncio
    async def test_get_waiver_log_success(self):
        """Test successful waiver log retrieval, end-to-end through the adapter."""
        mock_response = _make_espn_transactions_response([
            ("1", "WAIVER", [(111, 5)], []),
        ])

        async def mock_get_espn_transactions(league_id, week=None, types=None, year=None):
            return mock_response

        with patch('nfl_mcp.waiver_tools.get_espn_transactions', side_effect=mock_get_espn_transactions):
            result = await get_waiver_log("league1", week=3, year=2026, dedupe=True)

            assert result["success"] is True
            assert result["week"] == 3
            assert result["year"] == 2026
            assert len(result["waiver_log"]) == 1
            assert result["waiver_log"][0]["adds"] == {"111": 5}

    @pytest.mark.asyncio
    async def test_get_waiver_log_failed_fetch(self):
        """Test waiver log with failed transaction fetch."""
        mock_response = {"success": False, "error": "Failed to fetch"}

        async def mock_get_espn_transactions(league_id, week=None, types=None, year=None):
            return mock_response

        with patch('nfl_mcp.waiver_tools.get_espn_transactions', side_effect=mock_get_espn_transactions):
            result = await get_waiver_log("league1")

            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_get_waiver_log_no_dedup(self):
        """Test waiver log without deduplication."""
        mock_response = _make_espn_transactions_response([
            ("1", "WAIVER", [(111, 5)], []),
        ])

        async def mock_get_espn_transactions(league_id, week=None, types=None, year=None):
            return mock_response

        with patch('nfl_mcp.waiver_tools.get_espn_transactions', side_effect=mock_get_espn_transactions):
            result = await get_waiver_log("league1", dedupe=False)

            assert result["success"] is True
            assert result.get("deduplication_enabled") is False

    @pytest.mark.asyncio
    async def test_get_waiver_log_requests_waiver_and_freeagent_types(self):
        """get_waiver_log asks ESPN for WAIVER/FREEAGENT transactions directly."""
        mock_response = _make_espn_transactions_response([])
        captured = {}

        async def mock_get_espn_transactions(league_id, week=None, types=None, year=None):
            captured["types"] = types
            return mock_response

        with patch('nfl_mcp.waiver_tools.get_espn_transactions', side_effect=mock_get_espn_transactions):
            await get_waiver_log("league1", week=2, year=2026)

            assert captured["types"] == ["WAIVER", "FREEAGENT"]


class TestCheckReEntryStatus:
    """Test check_re_entry_status async function."""

    @pytest.mark.asyncio
    async def test_check_re_entry_status_success(self):
        """Test successful re-entry status check, end-to-end through the adapter."""
        mock_response = _make_espn_transactions_response([
            ("1", "WAIVER", [(111, 5)], []),
            ("2", "WAIVER", [], [(111, 5)]),
            ("3", "WAIVER", [(111, 5)], []),
        ])

        async def mock_get_espn_transactions(league_id, week=None, types=None, year=None):
            return mock_response

        with patch('nfl_mcp.waiver_tools.get_espn_transactions', side_effect=mock_get_espn_transactions):
            result = await check_re_entry_status("league1")

            assert result["success"] is True
            assert "111" in result["re_entry_players"]

    @pytest.mark.asyncio
    async def test_check_re_entry_status_failed_fetch(self):
        """Test re-entry status check with failed fetch."""
        mock_response = {"success": False, "error": "Failed to fetch"}

        async def mock_get_espn_transactions(league_id, week=None, types=None, year=None):
            return mock_response

        with patch('nfl_mcp.waiver_tools.get_espn_transactions', side_effect=mock_get_espn_transactions):
            result = await check_re_entry_status("league1")

            assert result["success"] is False


class TestGetWaiverWireDashboard:
    """Test get_waiver_wire_dashboard async function."""

    @pytest.mark.asyncio
    async def test_dashboard_success(self):
        """Test successful waiver wire dashboard generation."""
        mock_waiver_log = {
            "success": True,
            "waiver_log": [],
            "total_transactions": 5,
            "unique_transactions": 4
        }

        mock_re_entry = {
            "success": True,
            "re_entry_players": {},
            "volatile_players": [],
            "total_players_analyzed": 3,
            "players_with_re_entries": 1
        }

        async def mock_get_waiver_log(league_id, week=None, year=None, dedupe=True):
            return mock_waiver_log

        async def mock_check_re_entry(league_id, week=None, year=None):
            return mock_re_entry

        with patch('nfl_mcp.waiver_tools.get_waiver_log', side_effect=mock_get_waiver_log):
            with patch('nfl_mcp.waiver_tools.check_re_entry_status', side_effect=mock_check_re_entry):
                result = await get_waiver_wire_dashboard("league1", week=3, year=2026)

                assert result["success"] is True
                assert "waiver_log" in result
                assert "re_entry_analysis" in result
                assert "dashboard_summary" in result
                assert "total_waiver_transactions" in result["dashboard_summary"]
                assert result["week"] == 3
                assert result["year"] == 2026

    @pytest.mark.asyncio
    async def test_dashboard_failed_waiver_log(self):
        """Test dashboard with failed waiver log fetch."""
        mock_waiver_log = {
            "success": False,
            "error": "Failed"
        }

        async def mock_get_waiver_log(league_id, week=None, year=None, dedupe=True):
            return mock_waiver_log

        with patch('nfl_mcp.waiver_tools.get_waiver_log', side_effect=mock_get_waiver_log):
            result = await get_waiver_wire_dashboard("league1")

            assert result["success"] is False
