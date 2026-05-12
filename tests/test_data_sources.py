import pytest
from unittest.mock import patch, MagicMock


class TestQueryFishbase:
    def test_query_popgrowth(self):
        from ewe_mcp.data_sources.fishbase import query_fishbase

        species_row = [(1, "Gadus", "morhua", "Atlantic cod")]
        pg_rows = [(115.0, 0.1, None, 0.2, "tagging-recapture data")]

        def fake_sql(query):
            m = MagicMock()
            if "popgrowth" in query.lower():
                m.fetchall.return_value = pg_rows
            else:
                m.fetchall.return_value = species_row
            return m

        with patch("ewe_mcp.data_sources.fishbase.duckdb") as mock_duckdb:
            mock_duckdb.sql.side_effect = fake_sql
            result = query_fishbase("Gadus", "morhua", tables=["popgrowth"])

        assert "popgrowth" in result
        assert len(result["popgrowth"]) == 1
        assert result["popgrowth"][0]["Loo"] == 115.0
        assert result["popgrowth"][0]["M"] == 0.2

    def test_query_ecology(self):
        from ewe_mcp.data_sources.fishbase import query_fishbase

        species_row = [(1, "Gadus", "morhua", "Atlantic cod")]
        eco_rows = [(4.09, 0.18, 4.29, 1.0, "hunting macrofauna (predator)")]

        def fake_sql(query):
            m = MagicMock()
            if "ecology" in query.lower():
                m.fetchall.return_value = eco_rows
            else:
                m.fetchall.return_value = species_row
            return m

        with patch("ewe_mcp.data_sources.fishbase.duckdb") as mock_duckdb:
            mock_duckdb.sql.side_effect = fake_sql
            result = query_fishbase("Gadus", "morhua", tables=["ecology"])

        assert "ecology" in result
        assert result["ecology"][0]["DietTroph"] == 4.09
        assert result["ecology"][0]["FeedingType"] == "hunting macrofauna (predator)"

    def test_query_diet(self):
        from ewe_mcp.data_sources.fishbase import query_fishbase

        species_row = [(1, "Gadus", "morhua", "Atlantic cod")]
        diet_code_rows = [(1,)]
        diet_item_rows = [
            ("nekton", "finfish", "bony fish", "juv./adults", 18.1, "Trisopterus minutus"),
        ]

        def fake_sql(query):
            m = MagicMock()
            if "diet_items" in query.lower():
                m.fetchall.return_value = diet_item_rows
            elif "diet" in query.lower():
                m.fetchall.return_value = diet_code_rows
            else:
                m.fetchall.return_value = species_row
            return m

        with patch("ewe_mcp.data_sources.fishbase.duckdb") as mock_duckdb:
            mock_duckdb.sql.side_effect = fake_sql
            result = query_fishbase("Gadus", "morhua", tables=["diet"])

        assert "diet" in result
        assert result["diet"][0]["FoodI"] == "nekton"
        assert result["diet"][0]["DietPercent"] == 18.1

    def test_query_nonexistent_species(self):
        from ewe_mcp.data_sources.fishbase import query_fishbase

        def fake_sql(query):
            m = MagicMock()
            m.fetchall.return_value = []
            return m

        with patch("ewe_mcp.data_sources.fishbase.duckdb") as mock_duckdb:
            mock_duckdb.sql.side_effect = fake_sql
            result = query_fishbase("Nonexistent", "species")

        assert result.get("popgrowth") == []
        assert result.get("ecology") == []
        assert result.get("diet") == []

    def test_query_invalid_table(self):
        from ewe_mcp.data_sources.fishbase import query_fishbase

        with pytest.raises(ValueError, match="Invalid table"):
            query_fishbase("Gadus", "morhua", tables=["nonexistent_table"])

    def test_query_network_error(self):
        from ewe_mcp.data_sources.fishbase import query_fishbase
        import duckdb as real_duckdb

        with patch("ewe_mcp.data_sources.fishbase.duckdb") as mock_duckdb:
            mock_duckdb.sql.side_effect = real_duckdb.IOException("Network error")
            result = query_fishbase("Gadus", "morhua")

        assert "error" in result


class TestQuerySealifebase:
    def test_uses_slb_prefix(self):
        from ewe_mcp.data_sources.sealifebase import query_sealifebase

        species_row = [(57969, "Abdopus", "horridus", "Red S. octopus")]
        eco_rows = [(3.5, 0.2, 3.8, 0.5, "predator")]
        queries_run = []

        def fake_sql(query):
            queries_run.append(query)
            m = MagicMock()
            if "ecology" in query.lower():
                m.fetchall.return_value = eco_rows
            else:
                m.fetchall.return_value = species_row
            return m

        with patch("ewe_mcp.data_sources.sealifebase.duckdb") as mock_duckdb:
            mock_duckdb.sql.side_effect = fake_sql
            result = query_sealifebase("Abdopus", "horridus", tables=["ecology"])

        assert "ecology" in result
        assert any("slb/" in q for q in queries_run), f"Expected 'slb/' in queries: {queries_run}"


class TestQueryGlobi:
    def test_query_preys_on(self):
        from ewe_mcp.data_sources.globi import query_globi

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "columns": [
                "source_taxon_name",
                "interaction_type",
                "target_taxon_name",
                "latitude",
                "longitude",
                "study_title",
            ],
            "data": [
                ["Gadus morhua", "preysOn", "Alosa pseudoharengus", None, None, "study 1"],
                ["Gadus morhua", "preysOn", "Clupea harengus", 55.0, -10.0, None],
            ],
        }

        with patch("ewe_mcp.data_sources.globi.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_response
            result = query_globi("Gadus morhua", interaction_type="preysOn")

        assert result["source"] == "Gadus morhua"
        assert result["interaction_type"] == "preysOn"
        assert len(result["interactions"]) == 2
        assert result["interactions"][0]["target"] == "Alosa pseudoharengus"
        assert result["interactions"][1]["latitude"] == 55.0

    def test_query_eaten_by(self):
        from ewe_mcp.data_sources.globi import query_globi

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "columns": [
                "source_taxon_name",
                "interaction_type",
                "target_taxon_name",
                "latitude",
                "longitude",
                "study_title",
            ],
            "data": [
                ["Gadus morhua", "eatenBy", "Stercorarius skua", None, None, None],
            ],
        }

        with patch("ewe_mcp.data_sources.globi.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_response
            result = query_globi("Gadus morhua", interaction_type="eatenBy")

        assert result["interaction_type"] == "eatenBy"
        assert result["interactions"][0]["target"] == "Stercorarius skua"

    def test_query_no_interactions(self):
        from ewe_mcp.data_sources.globi import query_globi

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "columns": [
                "source_taxon_name",
                "interaction_type",
                "target_taxon_name",
                "latitude",
                "longitude",
                "study_title",
            ],
            "data": [],
        }

        with patch("ewe_mcp.data_sources.globi.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_response
            result = query_globi("Nonexistent species")

        assert result["interactions"] == []
        assert result["count"] == 0

    def test_query_network_error(self):
        from ewe_mcp.data_sources.globi import query_globi
        import httpx as real_httpx

        with patch("ewe_mcp.data_sources.globi.httpx") as mock_httpx:
            mock_httpx.get.side_effect = real_httpx.ConnectError("Connection refused")
            result = query_globi("Gadus morhua")

        assert "error" in result

    def test_query_capping(self):
        from ewe_mcp.data_sources.globi import query_globi

        data = [
            ["Gadus morhua", "preysOn", f"Prey {i}", None, None, None]
            for i in range(150)
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "columns": [
                "source_taxon_name",
                "interaction_type",
                "target_taxon_name",
                "latitude",
                "longitude",
                "study_title",
            ],
            "data": data,
        }

        with patch("ewe_mcp.data_sources.globi.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_response
            result = query_globi("Gadus morhua")

        assert len(result["interactions"]) == 100
        assert "Showing 100 of 150" in result["note"]
