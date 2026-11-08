"""Chart boundaries, uncertainty, and publication over isolated history."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import unittest
import webbrowser
from contextlib import closing
from io import StringIO
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo
from urllib.parse import unquote, urlsplit


from scripts.lib import conf
from scripts.buildlog import charts, costs, index, moments, record
from scripts.buildlog import test_moments as fixtures
from scripts.buildlog.test_holds import hold_event
from scripts.buildlog.test_index import Record, call, ci_job, ci_run
from scripts.lint.memory_test_support import write_config_fixture


REVIEW_HOST = "100.100.100.100"


def assert_review_link(test: unittest.TestCase, stdout: str, reviews: Path, cache: Path) -> str:
    lines = stdout.splitlines()
    test.assertEqual(len(lines), 1)
    link = urlsplit(lines[0])
    test.assertEqual(link.scheme, "http")
    test.assertEqual(link.hostname, REVIEW_HOST)
    test.assertTrue(all(str(cache) not in line for line in lines))
    snapshot = reviews / unquote(link.path.lstrip("/"))
    test.assertEqual(snapshot.read_bytes(), (cache / "buildlog/charts/latest.html").read_bytes())
    return lines[0]


class ChartFixture(unittest.TestCase):
    fixture: fixtures.MomentTests  # pyright: ignore[reportUninitializedInstanceVariable]

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.targets: mock.Mock = mock.Mock(return_value=())

    @override
    def setUp(self) -> None:
        self.fixture = fixtures.moment_fixture(self)
        self.fixture.fixture.root.mkdir(parents=True, exist_ok=True)
        _ = cast(object, self.enterContext(mock.patch.object(record, "configured_targets", self.targets)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "REVIEW_LINKS_ROOT": str(self.fixture.fixture.root / "review-links"),
            "REVIEW_LINKS_HOST": REVIEW_HOST,
        })))


class PageDataTests(ChartFixture):
    ZONE: ZoneInfo = ZoneInfo("America/New_York")

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": str(self.ZONE)})))
        clock = cast(mock.Mock, self.enterContext(mock.patch.object(charts, "datetime", wraps=datetime)))
        cast(mock.Mock, clock.now).return_value = fixtures.END.astimezone(self.ZONE)

    @property
    def config_file(self) -> Path:
        return Path(os.environ["CONF_DIR"]) / "buildlog.conf"

    def configure(self, key: str, value: str) -> None:
        _ = write_config_fixture(self.config_file, key + "=" + value)

    def rows(self, n: int = 3) -> tuple[charts.TopCost, ...]:
        result = charts.page_data(7, n)
        if not isinstance(result, charts.PageData) or isinstance(result.top.rows, charts.NoDataYet):
            self.fail(f"Expected ranked costs, received {result!r}")
        return result.top.rows

    def test_top_costs_ranks_and_limits_the_recent_window(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("old", -86400, target="old", mem_wait_s=900000),
            fixtures.build("wait", 10, mem_wait_s=7200),
            fixtures.build("failure", 20, target="cloud", status=1, duration_s=3600),
            fixtures.build("cold", 30, target="mac", crates_compiled=50, duration_s=1800),
        )):
            rows = self.rows(2)
        self.assertEqual([(row.machine, row.cause, row.recent) for row in rows], [
            ("natedev", "Memory wait", charts.MeasuredCost(7200)),
            ("cloud", "Failed runs", charts.MeasuredCost(3600)),
        ])

    def test_top_costs_preserves_missing_waits_and_the_mac_upper_bound(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 7200, mem_wait_s=3600, call_id="eligible"),
            fixtures.build("missing", 8000, target="cloud", mem_wait_s=None),
        )):
            rows = self.rows(10)
        values = {(row.machine, row.cause): row for row in rows}
        self.assertEqual(values["cloud", "Memory wait"].recent, charts.CostUnavailable("1 applicable waits missing"))
        self.assertEqual(values["mac", "Mac idle while natedev waits"].recent, charts.MacIdleUpperBound(0, 3600))
        self.assertEqual(values["cloud", "Memory wait"].window, charts.WindowMatchesRecent())
        self.assertEqual(values["mac", "Mac idle while natedev waits"].window, charts.WindowMatchesRecent())

    def test_window_comparison_uses_daily_rates_and_a_strict_half_threshold(self) -> None:
        with closing(self.fixture.connection(*[
            fixtures.build(machine + "-recent", 10, target=machine, mem_wait_s=7200)
            for machine in ("below", "equal", "above", "lower")
        ], *[
            fixtures.build(machine + "-old", -86400, target=machine, mem_wait_s=seconds)
            for machine, seconds in (("below", 30599), ("equal", 30600), ("above", 30601))
        ])):
            values = {row.machine: row.window for row in self.rows(10)}
        self.assertEqual(values["below"], charts.WindowMatchesRecent())
        self.assertEqual(values["equal"], charts.WindowMatchesRecent())
        self.assertEqual(values["above"], charts.WindowDiffers(charts.MeasuredCost(37801 / 7)))
        self.assertEqual(values["lower"], charts.WindowDiffers(charts.MeasuredCost(7200 / 7)))

    def test_page_window_preserves_older_missing_waits(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("missing", -86400, mem_wait_s=None),
            fixtures.build("recent", 10, mem_wait_s=3600),
        )):
            row = self.rows(1)[0]
        self.assertEqual(row.recent, charts.MeasuredCost(3600))
        self.assertEqual(row.window, charts.WindowDiffers(charts.CostUnavailable("1 applicable waits missing")))

    def test_window_comparison_preserves_unavailable_states(self) -> None:
        unknown = (charts.NoDataYet.VALUE, charts.CostUnavailable("missing coverage"))
        for recent in (charts.MeasuredCost(3600), charts.MacIdleUpperBound(3600, 7200)):
            for window in unknown:
                with self.subTest(recent=recent, window=window):
                    self.assertEqual(charts.window_value(recent, window, 2, 7), charts.WindowDiffers(window))
        for recent in unknown:
            with self.subTest(recent=recent):
                self.assertEqual(charts.window_value(recent, charts.MeasuredCost(3600), 2, 7), charts.WindowMatchesRecent())

    def test_empty_window_keeps_no_data_and_the_user_zone(self) -> None:
        with closing(self.fixture.connection(fixtures.build("old", -8 * 86400, mem_wait_s=9000))):
            data = charts.page_data(7)
        if not isinstance(data, charts.PageData):
            self.fail(f"Expected an empty window, received {data!r}")
        result = data.top
        self.assertEqual(result.rows, charts.NoDataYet.VALUE)
        self.assertEqual(result.start, fixtures.START.astimezone(self.ZONE))
        self.assertEqual(result.end, fixtures.END.astimezone(self.ZONE))
        self.assertEqual((result.start.tzinfo, result.end.tzinfo), (self.ZONE, self.ZONE))
        self.assertEqual(result.recent_days, 2)

    def test_actions_and_owners_read_the_fixture_config_at_use(self) -> None:
        self.configure("COST_ACTION_MEMORY_WAIT", "Reduce the fixture memory wait.")
        self.configure("COST_OWNER_MEMORY_WAIT", "fixture-owner")
        with closing(self.fixture.connection(fixtures.build("wait", 10, mem_wait_s=3600))):
            first = self.rows(1)[0]
            self.configure("COST_ACTION_MEMORY_WAIT", "Reduce the new fixture memory wait.")
            self.configure("COST_OWNER_MEMORY_WAIT", "new-fixture-owner")
            second = self.rows(1)[0]
        self.assertEqual(first.action, charts.CostAction("Reduce the fixture memory wait.", "fixture-owner"))
        self.assertEqual(second.action, charts.CostAction("Reduce the new fixture memory wait.", "new-fixture-owner"))

    def test_missing_or_empty_action_keys_name_the_config_error(self) -> None:
        source = self.config_file.read_text()
        with closing(self.fixture.connection(fixtures.build("wait", 10, mem_wait_s=3600))):
            for key in ("COST_ACTION_MEMORY_WAIT", "COST_OWNER_MEMORY_WAIT"):
                for missing in (True, False):
                    with self.subTest(key=key, missing=missing):
                        _ = self.config_file.write_text(source)
                        if missing:
                            _ = self.config_file.write_text("\n".join(
                                line for line in source.splitlines() if not line.startswith(key + "=")
                            ) + "\n")
                        else:
                            self.configure(key, "")
                        with self.assertRaisesRegex(conf.Unusable, key):
                            _ = charts.page_data(7)

    def test_page_data_reads_the_index_once_for_charts_and_costs(self) -> None:
        with closing(self.fixture.connection(fixtures.build("wait", 10, mem_wait_s=3600))), \
             mock.patch.object(costs, "read_costs", wraps=costs.read_costs) as read, \
             mock.patch.object(index, "read_only", wraps=index.read_only) as opened:
            row = self.rows(1)[0]
        self.assertEqual(row.recent, charts.MeasuredCost(3600))
        self.assertEqual(row.window, charts.WindowDiffers(charts.MeasuredCost(3600 / 7)))
        read.assert_called_once_with(mock.ANY, fixtures.END - timedelta(days=7), fixtures.END, boundary_context=True)
        opened.assert_called_once_with()

    def test_window_lengths_come_from_config(self) -> None:
        self.configure("TOP_COST_RECENT_DAYS", "1")
        with closing(self.fixture.connection(
            fixtures.build("recent", 90000, mem_wait_s=3600),
            fixtures.build("older", 10, mem_wait_s=7200),
            fixtures.build("outside", -3 * 86400, mem_wait_s=90000),
        )):
            result = charts.page_data(4, 1)
        if not isinstance(result, charts.PageData) or isinstance(result.top.rows, charts.NoDataYet):
            self.fail(f"Expected ranked costs, received {result!r}")
        rows = result.top.rows
        self.assertEqual(rows[0].recent, charts.MeasuredCost(3600))
        self.assertEqual(rows[0].window, charts.WindowMatchesRecent())

    def test_recent_window_cannot_exceed_the_page_days(self) -> None:
        self.configure("TOP_COST_RECENT_DAYS", "4")
        with closing(self.fixture.connection(fixtures.build("wait", 10, mem_wait_s=3600))):
            with self.assertRaisesRegex(conf.Unusable, r"TOP_COST_RECENT_DAYS.*3"):
                _ = charts.page_data(3)

    def test_page_data_matches_the_existing_chart_calculation(self) -> None:
        self.targets.return_value = ("natedev", "unused")
        with closing(self.fixture.connection(fixtures.build("wait", 10, mem_wait_s=3600))) as connection:
            end = fixtures.END.astimezone(self.ZONE)
            expected = charts.collect_charts(connection, end - timedelta(days=7), end, self.ZONE)
            actual = charts.page_data(7)
        if not isinstance(actual, charts.PageData):
            self.fail(f"Expected page data, received {actual!r}")
        self.assertEqual(actual.charts, expected)
        self.assertEqual(charts.cost_value(expected.buckets[0], "unused", "Memory wait"), charts.NoDataYet.VALUE)

    def test_page_data_returns_the_index_error(self) -> None:
        database = index.index_path()
        database.parent.mkdir(parents=True, exist_ok=True)
        for damage, content, reason in (
            ("absent", None, "unable to open database"),
            ("corrupt", b"This file is not a SQLite index.", "not a database"),
        ):
            if content is not None:
                _ = database.write_bytes(content)
            with self.subTest(damage=damage):
                result = charts.page_data(7)
                if not isinstance(result, charts.IndexUnavailable):
                    self.fail(f"Expected the index error, received {result!r}")
                self.assertIn(reason, result.reason)
                stderr = StringIO()
                with mock.patch("sys.stderr", stderr):
                    self.assertEqual(charts.main([]), 1)
                self.assertEqual(stderr.getvalue(), f"buildlog charts: index unavailable: {result.reason}\n")


class ChartTests(ChartFixture):
    def label_page(self) -> str:
        events = tuple(moments.BuildMoment(fixtures.START+timedelta(hours=1, seconds=number*10),
                                          f"Change {number}", "all") for number in range(4))
        self.fixture.progress([
            {"event_type": "phase_finished", "phase_instance_id": f"phase-{day}-{duration}",
             "working_dir": "/r/hana", "timestamp_epoch": fixtures.START.timestamp()+day*86400+3600,
             "phase_started_at": fixtures.START.timestamp()+day*86400+3600-duration}
            for day in range(2) for duration in (1200, 2400)
        ])
        coverage = tuple(costs.IndexedSourceCoverage(machine, fixtures.START.timestamp(), fixtures.END.timestamp())
                         for machine in ("natedev", "mac", "CI"))
        with closing(self.fixture.connection(*[
            fixtures.build(f"{machine}-{day}", day*86400+7200, target=machine,
                           mem_wait_s=3600*(day+1) if machine == "natedev" else 0)
            for machine in ("natedev", "mac", "CI") for day in range(2)
        ])) as connection, mock.patch.object(moments, "MOMENTS", events), \
             mock.patch.object(costs, "source_coverage", return_value=coverage):
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        return charts.render_html(series)

    def rendered_layout(self, page: str, probe: str, width: int) -> str:
        path = self.fixture.fixture.root/"label-layout.html"
        script = ("<script>const failures=[];"+probe+
                  ";const result=document.createElement('pre');result.id='layout-result';"+
                  "result.textContent=failures.length?failures.join('\\n'):'PASS';document.body.append(result);</script>")
        _ = path.write_text(page+script)
        result = subprocess.run([
            "google-chrome", "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--virtual-time-budget=300", f"--window-size={width},4000",
            "--dump-dom", path.as_uri(),
        ], capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'<pre id="layout-result">(.*?)</pre>', result.stdout, re.S)
        self.assertIsNotNone(match, "The browser did not complete the layout probe.")
        assert match is not None
        return html.unescape(match[1])

    def machine_label_page(self, values: dict[str, tuple[float, ...]]) -> str:
        setup = r"""
const source=data.buckets[0],day=86400000;
data.moments=[];data.machines=Object.keys(fixtureValues);
data.end=data.start+Object.values(fixtureValues)[0].length*day;
data.buckets=Object.values(fixtureValues)[0].map((_,i)=>({...structuredClone(source),
 start:data.start+i*day,end:data.start+(i+1)*day,
 label:new Date(data.start+i*day).toISOString().slice(0,10).replaceAll('-',''),
 rows:Object.entries(fixtureValues).map(([machine,values])=>({machine,cause:data.causes[0],
 daily:{state:'measured',value:values[i]},perPhase:{state:'measured',value:values[i]}}))}));
redraw();
"""
        return self.label_page()+"<script>const fixtureValues="+json.dumps(values)+";"+setup+"</script>"

    def test_rendered_close_endpoints_with_unequal_values_never_share_a_value_label(self) -> None:
        page = self.machine_label_page({"natedev": (1000, 100, 100), "mac": (0, 1, 1), "CI": (0, 1.5, 1.5)})
        # Keep the false group visible beside the higher natedev endpoint.
        page = page.replace("</head>", "<style>svg .end-label{font-size:9px}</style></head>")
        probe = r"""
const svg=document.getElementById('machinePlot'),labels=[...svg.querySelectorAll('.end-label')];
if(!labels.length)failures.push('Missing endpoint labels');
for(const label of labels){const members=JSON.parse(label.dataset.members),values=members.map(name=>fixtureValues[name].at(-1));
 if(values.some(value=>value!==values[0]))failures.push('Unequal values share a label: '+label.textContent)}
for(const name of ['mac','CI'])if(!document.getElementById('machineLegend').textContent.includes(name))failures.push('Missing legend identity');
if(!document.getElementById('machineTable').textContent.includes('1.5 h/day'))failures.push('The table lost the CI value');
"""
        for width in (1280, 500):
            with self.subTest(width=width):
                self.assertEqual(self.rendered_layout(page, probe, width), "PASS")

    def test_rendered_gap_notes_yield_to_endpoint_labels_when_their_boxes_collide(self) -> None:
        page = self.machine_label_page({"natedev": (4,)+(2,)*29, "mac": (1.6,)*30, "CI": (0,)*30})
        probe = r"""
const missing=data.buckets.at(-1).rows.find(row=>row.machine==='natedev');
missing.daily={state:'unavailable',reason:'missing fixture coverage'};redraw();
const svg=document.getElementById('machinePlot'),labels=[...svg.querySelectorAll('.end-label')];
for(const name of ['natedev','mac'])if(!labels.some(label=>JSON.parse(label.dataset.members).includes(name)))failures.push('Missing endpoint label: '+name);
const overlaps=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
for(const note of svg.querySelectorAll('.gap-label'))for(const label of labels)if(overlaps(note.getBoundingClientRect(),label.getBoundingClientRect()))failures.push('Gap note overlaps '+label.textContent);
if(!document.getElementById('machineTable').textContent.includes('missing fixture coverage'))failures.push('The table lost the gap reason');
"""
        for width in (1280, 500):
            with self.subTest(width=width):
                self.assertEqual(self.rendered_layout(page, probe, width), "PASS")

    def test_rendered_moment_numbers_have_a_band_clear_of_every_panel_title(self) -> None:
        page = self.label_page()
        probe = r"""
const titles=[...document.querySelectorAll('.panel-title')],numbers=[...document.querySelectorAll('.moment-number')];
if(titles.length!==6||numbers.length!==32)failures.push('Missing fixture titles or moment numbers');
function overlaps(a,b){return a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top}
for(const number of numbers)for(const title of titles)if(overlaps(number.getBoundingClientRect(),title.getBoundingClientRect()))failures.push('Moment '+number.textContent+' overlaps '+title.textContent);
for(const panel of document.querySelectorAll('.plot-panel')){const marks=[...panel.querySelectorAll('.moment-number')];for(let i=0;i<marks.length;i++)for(let j=i+1;j<marks.length;j++)if(overlaps(marks[i].getBoundingClientRect(),marks[j].getBoundingClientRect()))failures.push('Moment numbers overlap')}
"""
        for width in (1280, 500):
            with self.subTest(width=width):
                self.assertEqual(self.rendered_layout(page, probe, width), "PASS")

    def test_rendered_end_labels_stay_nearest_their_series_and_group_equal_endpoints(self) -> None:
        page = self.label_page()
        probe = r"""
for(const svg of document.querySelectorAll('#phase,#machinePlot')){
 const ends=new Map();for(const mark of svg.querySelectorAll('.series-point'))ends.set(mark.dataset.series,mark);
 const labels=[...svg.querySelectorAll('.end-label')];if(!ends.size||!labels.length)failures.push('Missing rendered endpoints or labels');
 const distance=(label,point)=>Math.hypot(Number(label.getAttribute('x'))-Number(point.getAttribute('cx')),Number(label.getAttribute('y'))-Number(point.getAttribute('cy')));
 for(const label of labels){const members=JSON.parse(label.dataset.members||'[]');if(!members.length)failures.push('Missing label identities');
 for(const member of members){const own=ends.get(member);if(!own||!label.firstChild.textContent.includes(member)){failures.push('Missing visible member '+member);continue}const nearest=Math.min(...[...ends.values()].map(point=>distance(label,point)));if(distance(label,own)>nearest+.1)failures.push(member+' label is nearer another endpoint')}
 }
}
const machine=document.getElementById('machinePlot'),group=[...machine.querySelectorAll('.end-label')].find(label=>label.firstChild.textContent==='mac, CI · 0');
if(!group)failures.push('Equal zero endpoints need one named group');
if(!machine.querySelector('.overlap-mark[data-series="mac"]')||!machine.querySelector('.overlap-mark[data-series="CI"]'))failures.push('An overlapping series has no visible mark');
for(const mark of machine.querySelectorAll('.overlap-mark'))if(mark.getAttribute('stroke-width')!=='2')failures.push('Missing surface ring');
"""
        for width in (1280, 500):
            with self.subTest(width=width):
                self.assertEqual(self.rendered_layout(page, probe, width), "PASS")

    def test_passed_tree_pairs_cross_midnight_and_the_first_edge(self) -> None:
        records = [fixtures.build("context", -60, duration_s=120, session="same", tree_key="tree", target="natedev"),
                   fixtures.build("first", 90, duration_s=60, session="same", tree_key="tree", target="natedev"),
                   fixtures.build("before-midnight", 86300, duration_s=60, session="same", tree_key="tree", target="natedev"),
                   fixtures.build("after-midnight", 86410, duration_s=120, session="same", tree_key="tree", target="natedev")]
        with closing(self.fixture.connection(*records)) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        repeats = [[row.seconds for row in bucket.costs.ranked if row.cause == "Reruns after a pass"] for bucket in series.buckets]
        self.assertEqual(repeats, [[60], [120]])
        self.assertEqual(sum(row.seconds for bucket in series.buckets for row in bucket.costs.ranked
                             if row.cause == "Memory wait"), 0)

    def test_context_uses_the_last_run_for_each_machine_and_session(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("old-pass", -600, target="natedev", session="shared", tree_key="tree"),
            fixtures.build("latest-failure", -120, target="natedev", session="shared", tree_key="tree", status=1),
            fixtures.build("other-machine", -60, target="mac", session="shared", tree_key="tree"),
            fixtures.build("local", 60, target="natedev", session="shared", tree_key="tree"),
            fixtures.build("remote", 120, target="mac", session="shared", tree_key="tree", duration_s=120),
        )) as connection:
            reading = costs.read_costs(connection, fixtures.START, fixtures.END, boundary_context=True)
        self.assertEqual([(row.machine, row.seconds) for row in reading.contributions
                          if row.cause == "Reruns after a pass"], [("mac", 120)])

    def test_window_and_context_plans_search_indexes_without_history_scans(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("before", -60, target="natedev", session="shared", tree_key="tree"),
            fixtures.build("after", 60, target="natedev", session="shared", tree_key="tree"),
        )) as connection:
            for context in (False, True):
                with self.subTest(boundary_context=context):
                    statements: list[str] = []
                    connection.set_trace_callback(statements.append)
                    _ = costs.read_costs(connection, fixtures.START, fixtures.END, boundary_context=context)
                    connection.set_trace_callback(None)
                    query = next(sql for sql in statements if sql.startswith("WITH window_steps"))
                    plan = cast(list[tuple[int, int, int, str]],
                                connection.execute("EXPLAIN QUERY PLAN " + query).fetchall())
                    details = [row[3] for row in plan]
                    self.assertTrue(any("SEARCH steps USING INDEX steps_started" in row for row in details), details)
                    self.assertTrue(any("SEARCH calls USING COVERING INDEX calls_copy" in row for row in details), details)
                    self.assertFalse(any(row.startswith(("SCAN steps", "SCAN prior", "SCAN calls", "SCAN main.calls"))
                                         for row in details), details)
                    if context:
                        self.assertTrue(any("SEARCH prior USING INDEX steps_session_previous" in row for row in details), details)
                        self.assertTrue(any("steps_execution_end" in row for row in details), details)
                        self.assertTrue(any("steps_wait_started" in row for row in details), details)

    def test_execution_from_before_window_covers_wait_across_midnight(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("mac", -3600, target="mac", duration_s=93600),
            fixtures.build("wait", 93600, target="natedev", mem_wait_s=90000, call_id="eligible"),
        )) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual([b.costs.mac_idle.candidate_wait_seconds for b in series.buckets], [82800, 7200])
        self.assertEqual([b.costs.mac_idle.execution_overlap_seconds for b in series.buckets], [82800, 3600])
        self.assertEqual([b.costs.mac_idle.upper_bound_seconds for b in series.buckets], [0, 3600])

    def test_parallel_waits_split_without_duplicate_seconds(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("a", 90000, target="natedev", mem_wait_s=7200, call_id="eligible"),
            fixtures.build("b", 91800, target="natedev", mem_wait_s=7200, call_id="eligible"),
        )) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual([b.costs.mac_idle.upper_bound_seconds for b in series.buckets], [3600, 5400])
        self.assertEqual([sum(r.seconds for r in b.costs.ranked if r.cause == "Memory wait") for b in series.buckets], [0, 14400])

    def test_complete_holds_split_and_unmatched_starts_keep_the_unknown_band(self) -> None:
        origin = fixtures.START
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 93600, target="natedev", mem_wait_s=14400, call_id="eligible"),
            hold_event("began", "lost", (origin+timedelta(seconds=79000)).isoformat(), "mac"),
            hold_event("began", "lost", (origin+timedelta(seconds=85000)).isoformat(), "mac"),
            hold_event("ended", "lost", (origin+timedelta(seconds=88000)).isoformat(), "mac"),
        )) as connection, mock.patch.object(costs, "source_coverage", return_value=(
            costs.IndexedSourceCoverage("mac", origin.timestamp(), fixtures.END.timestamp()),
        )), mock.patch.object(costs, "first_source_times", return_value={"mac": origin.timestamp()}):
            series = charts.collect_charts(connection, origin, fixtures.END, ZoneInfo("UTC"))
        idle = [b.costs.mac_idle for b in series.buckets]
        self.assertEqual([i.recorded_hold_seconds for i in idle], [1400, 1600])
        self.assertEqual([i.unknown_seconds for i in idle], [5800, 0])
        value = charts.cost_value(series.buckets[0], "mac", "Mac idle while natedev waits")
        self.assertEqual(value, charts.MacIdleUpperBound(0, 5800))
        page = charts.render_html(series)
        self.assertIn("Hatched bands show unknown idle time", page)
        self.assertIn('"state": "upper bound"', page)

    def test_missing_pair_fields_never_create_a_false_rerun(self) -> None:
        for missing in ({"session": None}, {"tree_key": None}, {"status": None}, {"duration_s": None}):
            with self.subTest(missing=missing), closing(self.fixture.connection(
                fixtures.build("before", -60, **{"session":"same", "tree_key":"tree", "target":"natedev", **missing}),
                fixtures.build("after", 60, session="same", tree_key="tree", target="natedev"),
            )) as connection:
                result = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
                self.assertEqual([row for b in result.buckets for row in b.costs.ranked if row.cause == "Reruns after a pass"], [])

    def test_exact_edges_assign_steps_phases_ci_and_moments_to_the_later_bucket(self) -> None:
        edge = fixtures.START+timedelta(days=1)
        self.fixture.progress([{"event_type": "phase_finished", "phase_instance_id": "edge", "working_dir": "/r/hana",
                                "timestamp_epoch": edge.timestamp(), "phase_started_at": edge.timestamp()-60}])
        job = ci_job(1, "build", "cancelled", (edge.isoformat(), edge.isoformat(), (edge+timedelta(hours=1)).isoformat()))
        run = ci_run(1, 1, [job])
        run.update(started_at=edge.isoformat(), created_at=edge.isoformat(), updated_at=(edge+timedelta(hours=1)).isoformat(), conclusion="cancelled")
        events: tuple[moments.Moment, ...] = (moments.BuildMoment(edge, "edge", "all"),
            moments.MemoryMoment(fixtures.END, "excluded"), moments.PendingMemoryMoment("pending"))
        with closing(self.fixture.connection(
            run, fixtures.build("at-edge", 86400, mem_wait_s=120, target="natedev"),
            fixtures.build("at-end", 172800, mem_wait_s=999, target="natedev"),
        )) as connection, mock.patch.object(moments, "MOMENTS", events):
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual([len(b.costs.phases) for b in series.buckets], [0, 1])
        self.assertEqual([sum(r.seconds for r in b.costs.ranked if r.cause == "Cancelled CI") for b in series.buckets], [0, 3600])
        self.assertEqual([sum(r.seconds for r in b.costs.ranked if r.cause == "Memory wait") for b in series.buckets], [0, 120])
        data = charts.chart_data(series)
        self.assertEqual(data["moments"], [{"at": edge.timestamp()*1000, "name": "edge", "scope": "all", "local": "Oct 11 00:00 UTC"}])
        self.assertEqual(data["pending"], ["pending: not live yet"])

    def test_calendar_edges_keep_dst_duration_and_partial_buckets(self) -> None:
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 10, 31, 12, tzinfo=zone).astimezone(UTC)
        end = datetime(2026, 11, 2, 12, tzinfo=zone).astimezone(UTC)
        edges = charts.bucket_edges(start, end, zone)
        self.assertEqual([(b-a).total_seconds()/3600 for a,b in zip(edges, edges[1:])], [12, 25, 12])
        reading = costs.CostReading((costs.CostContribution(edges[1].timestamp(), "cloud", "Memory wait", 90000),),
                                    (costs.SourceObservation(edges[1].timestamp(), "cloud"),), (), ())
        series = charts.build_series(reading, start, end, zone)
        self.assertEqual([b.thin for b in series.buckets], [True, False, True])
        value = charts.cost_json(charts.cost_value(series.buckets[1], "cloud", "Memory wait"), 3600*25/24)
        self.assertEqual(value, {"state": "measured", "value": 24})
        spring = charts.bucket_edges(datetime(2026,3,8,5,tzinfo=UTC), datetime(2026,3,9,4,tzinfo=UTC), zone)
        self.assertEqual((spring[1]-spring[0]).total_seconds(), 23*3600)

    def test_identical_starts_use_the_existing_id_order(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("a", 100, duration_s=60, target="natedev", session="same", tree_key="tree"),
            fixtures.build("b", 100, duration_s=120, target="natedev", session="same", tree_key="tree"),
        )) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual([r.seconds for b in series.buckets for r in b.costs.ranked if r.cause == "Reruns after a pass"], [120])

    def test_phase_gaps_keep_missing_starts_and_per_phase_unavailable(self) -> None:
        self.fixture.progress([
            {"event_type": "phase_finished", "phase_instance_id": "missing", "working_dir": "/r/hana", "timestamp_epoch": fixtures.START.timestamp()},
        ])
        with closing(self.fixture.connection(fixtures.build("record", 10, target="natedev", mem_wait_s=3600))) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        buckets = cast(list[dict[str, object]], charts.chart_data(series)["buckets"])
        self.assertEqual(buckets[0]["phase"], {"finished": 1, "valid": 0, "missing": 1, "state": "unavailable", "reason": "no valid durations"})
        self.assertEqual(buckets[1]["phase"], {"finished": 0, "valid": 0, "missing": 0, "state": "unavailable", "reason": "no finished phases"})
        self.assertEqual(charts.cost_value(series.buckets[1], "natedev", "Memory wait", per_phase=True),
                         charts.CostUnavailable("no finished phases"))
        self.assertEqual(charts.cost_value(series.buckets[0], "natedev", "Memory wait", per_phase=True), charts.MeasuredCost(3600))

    def test_unknown_machine_coverage_and_recorded_zero_are_distinct(self) -> None:
        with closing(self.fixture.connection(fixtures.build("cloud", 10, host="new-cloud", target="aws-new", mem_wait_s=0))) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual(series.machines, ("aws-new",))
        self.assertEqual(charts.cost_value(series.buckets[0], "aws-new", "Memory wait"), charts.MeasuredCost(0))
        self.assertEqual(charts.cost_value(series.buckets[1], "aws-new", "Memory wait"), charts.CostUnavailable("no recorded source coverage"))

    def monthly_mac_log(self, *records: Record) -> None:
        self.fixture.fixture.write(*records)
        path = self.fixture.fixture.root/"mac/2026-10.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = (self.fixture.fixture.root/"natedev/profiles.jsonl").replace(path)
        _ = index.update()

    def test_complete_indexed_mac_log_proves_zero_on_a_day_without_steps(self) -> None:
        self.monthly_mac_log(fixtures.build("before", 10, target="mac"),
                             fixtures.build("after", 172810, target="mac"))
        with closing(index.read_only()) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END+timedelta(days=1), ZoneInfo("UTC"))
        self.assertEqual(charts.cost_value(series.buckets[1], "mac", "Memory wait"), charts.MeasuredCost(0))
        self.assertEqual(charts.cost_value(series.buckets[1], "mac", "Failed runs"), charts.MeasuredCost(0))

    def test_missing_or_unindexed_mac_log_keeps_the_gap_and_names_the_source(self) -> None:
        self.monthly_mac_log(fixtures.build("before", 10, target="mac"),
                             fixtures.build("after", 172810, target="mac"))
        path = self.fixture.fixture.root/"mac/2026-10.jsonl"
        with path.open("ab") as source:
            _ = source.write(b"{}\n")
        for state in ("unindexed", "missing"):
            with self.subTest(source=state), closing(index.read_only()) as connection:
                series = charts.collect_charts(connection, fixtures.START, fixtures.END+timedelta(days=1), ZoneInfo("UTC"))
                self.assertIsInstance(charts.cost_value(series.buckets[1], "mac", "Memory wait"), charts.CostUnavailable)
                buckets = cast(list[dict[str, object]], charts.chart_data(series)["buckets"])
                rows = cast(list[dict[str, object]], buckets[1]["rows"])
                row = next(row for row in rows if row["machine"] == "mac" and row["cause"] == "Memory wait")
                self.assertEqual(row["daily"], {"state": "unavailable", "reason": "no complete indexed mac monthly log covers this day"})
            path.unlink(missing_ok=True)

    def test_bucket_sums_match_shared_costs_and_each_source_reads_once(self) -> None:
        self.fixture.progress([{"event_type":"phase_finished", "phase_instance_id":"one", "working_dir":"/r/hana",
                                "timestamp_epoch":fixtures.START.timestamp()+1, "phase_started_at":fixtures.START.timestamp()}])
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 90000, mem_wait_s=7200, call_id="eligible", target="natedev", crates_compiled=50),
        )) as connection:
            expected = costs.collect_costs(connection, fixtures.START, fixtures.END)
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            before = index.index_path().read_bytes()
            with mock.patch.object(costs, "read_costs", wraps=costs.read_costs) as shared, \
                 mock.patch.object(moments, "progress_events", wraps=moments.progress_events) as progress, \
                 mock.patch.object(costs, "mac_hold_events", wraps=costs.mac_hold_events) as holds:
                series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
            self.assertEqual(shared.call_count, 1)
            self.assertEqual(progress.call_count, 1)
            self.assertEqual(holds.call_count, 1)
            self.assertEqual(len([sql for sql in statements if sql.startswith("WITH window_steps")]), 1)
            self.assertEqual(len([sql for sql in statements if sql.startswith("SELECT r.conclusion")]), 1)
            self.assertEqual(index.index_path().read_bytes(), before)
        for row in expected.ranked:
            self.assertAlmostEqual(sum(r.seconds for b in series.buckets for r in b.costs.ranked
                                       if (r.machine, r.cause) == (row.machine, row.cause)), row.seconds)

    def test_browserless_cli_publishes_one_standalone_page_without_index_writes(self) -> None:
        with closing(self.fixture.connection(fixtures.build("record", 10, mem_wait_s=60))):
            pass
        cache = self.fixture.fixture.root/"cache"
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME":str(cache), "BUILDLOG_CHARTS_NO_OPEN":"1"}):
            result = fixtures.read_only_cli(self, ["charts", "--days", "7"])
        path = cache/"buildlog/charts/latest.html"
        _ = assert_review_link(self, result.stdout, self.fixture.fixture.root / "review-links", cache)
        page = path.read_text()
        self.assertEqual(page.count('<svg id='), 3)
        self.assertNotIn('<script src=', page)
        self.assertNotIn('https://', page)
        self.assertIn('prefers-color-scheme:dark', page)
        self.assertIn('role="img"', page)
        self.assertNotIn('@DATA@', page)

    def test_html_escapes_recorded_machine_and_moment_names(self) -> None:
        name = '</script><img src=x onerror=alert(1)>'
        at = fixtures.START+timedelta(hours=1)
        with closing(self.fixture.connection(fixtures.build("unsafe", 10, target=name))), \
             mock.patch.object(moments, "MOMENTS", (moments.BuildMoment(at, name, "all"),)):
            with closing(index.read_only()) as connection:
                page = charts.render_html(charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC")))
        self.assertNotIn(name, page)
        self.assertIn('\\u003c/script', page)

    def test_invalid_cli_window_never_opens_the_index(self) -> None:
        with mock.patch.object(index, "read_only", side_effect=AssertionError("index opened")):
            self.assertEqual(charts.main(["--days", "8"]), 2)

    def test_absent_mac_coverage_keeps_candidate_waits_unknown_in_both_views(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 7200, target="natedev", mem_wait_s=3600, call_id="eligible"),
        )) as connection:
            reading = costs.read_costs(connection, fixtures.START, fixtures.END)
        result = costs.aggregate_costs(reading, fixtures.START, fixtures.END)
        self.assertEqual(result.mac_idle.unknown_seconds, 3600)
        self.assertEqual(result.mac_idle.upper_bound_seconds, 3600)
        self.assertIn("upper bound; 0.50 h/day unknown", costs.render_costs(result))
        series = charts.build_series(reading, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual(charts.cost_value(series.buckets[0], "mac", "Mac idle while natedev waits"),
                         charts.MacIdleUpperBound(0, 3600))

    def test_applicable_missing_waits_keep_mixed_buckets_unavailable(self) -> None:
        for records, missing, expected in (
            ([fixtures.build("missing", 10, target="natedev", mem_wait_s=None)], 1, charts.CostUnavailable("1 applicable waits missing")),
            ([fixtures.build("zero", 10, target="natedev", mem_wait_s=0)], 0, charts.MeasuredCost(0)),
            ([fixtures.build("missing", 10, target="natedev", mem_wait_s=None),
              fixtures.build("measured", 20, target="natedev", mem_wait_s=60)], 1, charts.CostUnavailable("1 applicable waits missing")),
            ([fixtures.build("launch", 10, target="natedev", step="launch", mem_wait_s=None)], 0, charts.MeasuredCost(0)),
        ):
            fixture = fixtures.moment_fixture(self)
            with self.subTest(records=records), closing(fixture.connection(*records)) as connection:
                series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
                self.assertEqual(charts.cost_value(series.buckets[0], "natedev", "Memory wait"), expected)
                text = costs.render_costs(series.buckets[0].costs)
                self.assertEqual("applicable waits missing" in text, missing > 0)
                self.assertEqual(sum(row.missing_waits for row in series.buckets[1].costs.ranked), 0)

    def test_configured_machines_without_events_keep_named_unavailable_values(self) -> None:
        with mock.patch.object(record, "configured_targets", return_value=("natedev", "mac", "aws")), \
             mock.patch.object(costs, "first_source_times", return_value={machine: fixtures.START.timestamp()-1
                                                                       for machine in ("natedev", "mac", "aws")}), \
             closing(self.fixture.connection()) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual(series.machines, ("aws", "mac", "natedev"))
        for machine in series.machines:
            with self.subTest(machine=machine):
                self.assertEqual(charts.cost_value(series.buckets[0], machine, "Memory wait"),
                                 charts.CostUnavailable("no recorded source coverage"))
        self.assertIn('"aws"', charts.render_html(series))

    def configured_machine_series(self, *records: Record, targets: tuple[str, ...] = ("natedev", "aws"),
                                  start: datetime = fixtures.START, end: datetime = fixtures.END) -> charts.ChartSet:
        self.targets.return_value = targets
        with closing(self.fixture.connection(*records)) as connection:
            return charts.collect_charts(connection, start, end, ZoneInfo("UTC"))

    def test_all_machines_omit_a_configured_machine_until_its_first_source(self) -> None:
        series = self.configured_machine_series(
            fixtures.build("local", 10, target="natedev", mem_wait_s=3600),
            fixtures.build("later-local", 86410, target="natedev", mem_wait_s=1800),
            fixtures.build("later-cloud", 86420, target="aws", mem_wait_s=1800),
        )
        probe = r"""
const waits=causeSeries().find(series=>series.label==='Memory wait').values;
if(waits.some(point=>point.state!=='measured'||point.value!==1))failures.push('An inactive machine hides recorded costs');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def test_machine_without_a_log_has_one_message_and_a_named_selector_option(self) -> None:
        series = self.configured_machine_series(fixtures.build("local", 10, target="natedev", mem_wait_s=3600))
        probe = r"""
const emptyOption=[...document.getElementById('machine').options].find(option=>option.value==='aws');
if(!emptyOption?.textContent.includes('no data yet'))failures.push('The selector hides the missing source');
document.getElementById('machine').value='aws';redraw();
const svg=document.getElementById('cause');
if(svg.querySelectorAll('text').length!==1||svg.textContent!=='aws: no data yet'||svg.querySelector('.plot-panel'))failures.push('The machine needs one no-data message');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 500), "PASS")

    def test_all_machines_retain_missing_applicable_waits_with_an_inactive_target(self) -> None:
        series = self.configured_machine_series(
            fixtures.build("missing", 10, target="natedev", mem_wait_s=None),
            fixtures.build("recorded", 20, target="natedev", mem_wait_s=3600),
        )
        probe = self.memory_wait_unavailable_probe("1 applicable waits missing")
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def test_a_missing_historical_log_remains_required_without_steps_in_the_window(self) -> None:
        self.monthly_mac_log(fixtures.build("old-mac", -86400, target="mac"))
        self.fixture.fixture.write(fixtures.build("local", 10, target="natedev", mem_wait_s=3600))
        _ = index.update()
        (self.fixture.fixture.root/"mac/2026-10.jsonl").unlink()
        self.targets.return_value = ("natedev", "mac", "aws")
        with closing(index.read_only()) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        probe = self.memory_wait_unavailable_probe("no complete indexed mac monthly log covers this day")
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def memory_wait_unavailable_probe(self, reason: str) -> str:
        return ("const wait=causeSeries().find(series=>series.label==='Memory wait').values[0];"
                + "if(wait.state!=='unavailable'||wait.reason!==" + json.dumps(reason)
                + ")failures.push('The unavailable wait lost its reason');")

    def test_empty_indexed_monthly_log_keeps_the_machine_without_data(self) -> None:
        path = self.fixture.fixture.root/"aws/2026-11.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        start = datetime(2026, 11, 1, tzinfo=UTC)
        series = self.configured_machine_series(
            fixtures.build("local", 10, target="natedev", mem_wait_s=3600, started_at=(start+timedelta(seconds=10)).isoformat()),
            start=start, end=start+timedelta(days=1),
        )
        probe = r"""
const wait=causeSeries().find(series=>series.label==='Memory wait').values[0];
if(wait.state!=='measured'||wait.value!==1)failures.push('An empty log hides measured costs');
const emptyOption=[...document.getElementById('machine').options].find(option=>option.value==='aws');
if(!emptyOption?.textContent.includes('no data yet'))failures.push('An empty log activates its machine');
document.getElementById('machine').value='aws';redraw();
if(document.getElementById('cause').textContent!=='aws: no data yet')failures.push('The empty machine needs one message');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def test_other_group_omits_members_without_a_source_in_every_view(self) -> None:
        targets = ("natedev", "mac", "CI", "cloud-a", "cloud-b", "cloud-c", "cloud-d", "cloud-e", "cloud-f")
        series = self.configured_machine_series(
            fixtures.build("group-wait", 10, target="cloud-e", mem_wait_s=3600), targets=targets,
        )
        probe = r"""
document.getElementById('machine').value='Other';redraw();
const wait=causeSeries().find(series=>series.label==='Memory wait').values[0];
if(wait.state!=='measured'||wait.value!==1)failures.push('An inactive member hides its group');
if(!document.getElementById('causeTable').textContent.includes('1 h/day'))failures.push('The table hides its group');
const group=machineSeries().find(series=>series.label==='Other').values[0];
if(group.state!=='measured'||group.value!==1)failures.push('The machine plot hides its group');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 500), "PASS")

    def test_no_data_cells_agree_in_the_table_tooltip_and_detail(self) -> None:
        series = self.configured_machine_series(fixtures.build("local", 10, target="natedev", mem_wait_s=3600))
        probe = r"""
document.getElementById('machine').value='aws';redraw();
if(causeSeries().some(series=>series.values.some(point=>point.state!=='no data yet')))failures.push('The cell state differs');
for(const id of ['causeTable','causeDetail']){
 const text=document.getElementById(id).textContent;
 if(!text.includes('no data yet')||text.includes('no complete indexed aws'))failures.push(id+' hides the no-data state');
}
showTooltip(configs[1],0);
if(!document.getElementById('causeTooltip').textContent.includes('no data yet'))failures.push('The tooltip hides the no-data state');
if(document.getElementById('cause').viewBox.baseVal.width>1120)failures.push('The empty chart exceeds the width cap');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def test_cli_uses_the_user_zone_for_edges_and_moment_labels(self) -> None:
        event = moments.BuildMoment(datetime.now(UTC)-timedelta(hours=1), "zone marker", "all")
        with closing(self.fixture.connection()), \
             mock.patch.dict(os.environ, {"USER_ZONE": "UTC", "TZ": "America/Los_Angeles"}), \
             mock.patch.object(moments, "MOMENTS", (event,)), \
             mock.patch.object(charts, "publish") as publication, \
             mock.patch("scripts.buildlog.charts.review_link.snapshot", return_value=[f"http://{REVIEW_HOST}/zone/latest.html"]), \
             mock.patch.object(sys, "stdout", StringIO()):
            self.assertEqual(charts.main(["--no-open"]), 0)
        series = cast(charts.ChartSet, publication.call_args.args[0])
        self.assertEqual(series.zone, "UTC")
        self.assertTrue(all(bucket.start.hour == 0 for bucket in series.buckets[1:]))
        self.assertEqual(charts.chart_data(series)["moments"], [{
            "at": event.at.timestamp()*1000, "name": "zone marker", "scope": "all",
            "local": event.at.strftime("%b %d %H:%M UTC"),
        }])


class ChartOpenTests(ChartFixture):
    def test_local_display_excludes_every_ssh_session(self) -> None:
        cases: tuple[tuple[dict[str, str], str, bool], ...] = (
            ({}, "darwin", True),
            ({}, "linux", False),
            ({"DISPLAY": ":1"}, "linux", True),
            ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True),
            ({"DISPLAY": "", "WAYLAND_DISPLAY": ""}, "linux", False),
            ({"SSH_CONNECTION": "client 1 server 2"}, "darwin", False),
            ({"SSH_CONNECTION": ""}, "darwin", False),
            ({"SSH_CONNECTION": "client 1 server 2", "DISPLAY": ":1"}, "linux", False),
            ({"SSH_CONNECTION": "client 1 server 2", "WAYLAND_DISPLAY": "wayland-0"}, "linux", False),
            ({"SSH_CONNECTION": "client 1 server 2", "DISPLAY": ":1"}, "darwin", False),
        )
        for environment, platform, expected in cases:
            with self.subTest(environment=environment, platform=platform):
                self.assertEqual(charts.local_display(environment, platform), expected)

    def test_local_display_opens_the_printed_link(self) -> None:
        _ = self.check_publication({"DISPLAY": ":1"}, browser_opens=True)

    def test_sessions_without_a_local_display_print_the_link_without_opening(self) -> None:
        for environment in ({}, {"SSH_CONNECTION": "client 1 server 2", "DISPLAY": ":1"}):
            with self.subTest(environment=environment):
                _ = self.check_publication(environment, browser_opens=False)

    def test_browser_failures_preserve_the_printed_link(self) -> None:
        cache = self.fixture.fixture.root / "cache"
        path = cache / "buildlog/charts/latest.html"
        series = charts.ChartSet(fixtures.START, fixtures.END, "UTC", (), (), ())
        for result in (False, OSError("browser unavailable"), webbrowser.Error("browser unavailable")):
            with self.subTest(result=result):
                output, errors = StringIO(), StringIO()
                browser = mock.Mock(return_value=result) if isinstance(result, bool) else mock.Mock(side_effect=result)
                with mock.patch.dict(os.environ, {"DISPLAY": ":1"}), \
                     mock.patch.object(charts, "local_display", return_value=True), \
                     mock.patch.object(sys, "stdout", output), mock.patch.object(sys, "stderr", errors), \
                     mock.patch.object(webbrowser, "open", browser):
                    self.assertEqual(charts.present_page(series, path, False), 0)
                link = assert_review_link(self, output.getvalue(), self.fixture.fixture.root / "review-links", cache)
                browser.assert_called_once_with(link)
                self.assertEqual(errors.getvalue(), "")

    def test_no_open_controls_publish_only_the_link(self) -> None:
        for args, environment in ((("--no-open",), {}), ((), {"BUILDLOG_CHARTS_NO_OPEN": "1"})):
            with self.subTest(args=args, environment=environment):
                _ = self.check_publication({"DISPLAY": ":1", **environment}, browser_opens=False, args=args)

    def check_publication(self, environment: dict[str, str], *, browser_opens: bool,
                          args: tuple[str, ...] = ()) -> str:
        root = self.fixture.fixture.root
        reviews = root / "review-links"
        cache = root / "cache"
        output, errors = StringIO(), StringIO()
        browser = mock.Mock(return_value=True)
        settings = {name: value for name, value in os.environ.items()
                    if name not in ("SSH_CONNECTION", "DISPLAY", "WAYLAND_DISPLAY", "BUILDLOG_CHARTS_NO_OPEN")}
        settings.update({"XDG_CACHE_HOME": str(cache), **environment})
        with mock.patch.dict(os.environ, settings, clear=True), \
             mock.patch("scripts.buildlog.charts.sys.platform", "linux"), \
             mock.patch.object(sys, "stdout", output), mock.patch.object(sys, "stderr", errors), \
             mock.patch.object(webbrowser, "open", browser), closing(self.fixture.connection()):
            self.assertEqual(charts.main(list(args)), 0)
        link = assert_review_link(self, output.getvalue(), reviews, cache)
        if browser_opens:
            browser.assert_called_once_with(link)
        else:
            browser.assert_not_called()
        self.assertEqual(errors.getvalue(), "")
        return link

    def test_repeated_runs_print_the_same_link(self) -> None:
        first = self.check_publication({}, browser_opens=False, args=("--no-open",))
        second = self.check_publication({}, browser_opens=False, args=("--no-open",))
        self.assertEqual(first, second)

    def test_publish_failure_prints_one_reason_and_exits_nonzero(self) -> None:
        root = self.fixture.fixture.root
        reviews = root / "review-links"
        _ = reviews.write_text("A regular file blocks the review folder.")
        output, errors = StringIO(), StringIO()
        browser = mock.Mock()
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}), \
             mock.patch.object(sys, "stdout", output), mock.patch.object(sys, "stderr", errors), \
             mock.patch.object(webbrowser, "open", browser), closing(self.fixture.connection()):
            self.assertNotEqual(charts.main([]), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("publication failed:", errors.getvalue())
        self.assertIn(str(reviews), errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertTrue((root / "cache/buildlog/charts/latest.html").is_file())
        browser.assert_not_called()

    def test_unwritable_cache_prints_one_reason_and_exits_nonzero(self) -> None:
        cache = self.fixture.fixture.root / "cache"
        cache.mkdir(mode=0o500)
        self.addCleanup(cache.chmod, 0o700)
        output, errors = StringIO(), StringIO()
        browser = mock.Mock()
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}), \
             mock.patch.object(sys, "stdout", output), mock.patch.object(sys, "stderr", errors), \
             mock.patch.object(webbrowser, "open", browser), closing(self.fixture.connection()):
            self.assertEqual(charts.main([]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("Permission denied", errors.getvalue())
        self.assertIn("publication failed:", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        browser.assert_not_called()
