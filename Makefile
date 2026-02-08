.PHONY: run phase2-smoke
run:
	UV_CACHE_DIR=.uv-cache uv run python -m wealth_agents ingest --path inputs --source manual
	UV_CACHE_DIR=.uv-cache uv run python -m wealth_agents collect --config config/feeds.yml
	UV_CACHE_DIR=.uv-cache uv run python -m wealth_agents report --week $$(python3 -c "import datetime as d; y,w,_=d.date.today().isocalendar(); print(f'{y}-W{w:02d}')")

phase2-smoke:
	UV_CACHE_DIR=.uv-cache uv run pytest -q \
		tests/test_phase11_enhancements.py::test_report_persists_weekly_aggregates_and_renders_keyword_delta_table \
		tests/test_phase11_enhancements.py::test_top10_reduces_topic_bias_with_signature_clustering \
		tests/test_phase11_enhancements.py::test_korea_focus_empty_reason_explains_feed_failure_and_missing_manual \
		tests/test_collect_meta.py::test_collect_counts_two_success_one_failure_by_feed_status
