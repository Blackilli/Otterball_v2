"""Server-rendered geometry for the rank-over-time chart.

The SVG is built here rather than in the browser so the chart is readable with
no JavaScript at all - the lines, the axes and the player labels are in the
markup. The only thing script adds is the hover layer (crosshair and tooltip),
which is an enhancement, not the chart.

Why rank on the y axis and not points: points only ever go up, so a points
chart is a fan of rising lines that says little about who overtook whom. Rank
is the thing that actually moves.
"""

import datetime
from dataclasses import dataclass, field

from django.utils import timezone

from predictions.history import HistoryEntry, PlayerStanding

#: How many players the chart *names*: a label, a tooltip row and, for the
#: first few, a colour. Everyone else is still drawn, but as the faint band
#: behind them - see build_rank_chart.
CHART_SERIES = 10

#: Of those, how many carry a categorical colour. The rest are drawn in the
#: context grey and identified by their end-of-line label, so identity never
#: rests on colour alone - and five is inside the palette's comfortable cap.
CHART_HIGHLIGHTED = 5

WIDTH = 1000
PAD_TOP = 18
PAD_BOTTOM = 34
PAD_LEFT = 38
#: Room for the end-of-line player labels, which is what makes a legend
#: optional rather than load-bearing.
PAD_RIGHT = 156

#: Minimum vertical gap between two end-of-line labels before they are nudged
#: apart. Players tied on the final rank sit at the same y and would otherwise
#: print on top of each other.
LABEL_MIN_GAP = 12

#: Vertical room per rank. The chart grows with the range of ranks its players
#: actually held rather than using a fixed height: early in a season a leader
#: can sit 40th on one lucky pick, and a fixed box squashes the whole story
#: into its top eighth to accommodate that one excursion. This is also what
#: keeps the end-of-line labels on their own lines - below about this, they
#: get nudged apart and start pointing at the wrong player.
ROW_HEIGHT = 12

#: Minimum horizontal room between two date labels on the x axis.
X_TICK_MIN_GAP = 64

#: Bounds on the resulting plot, so a four-player pool is not a sliver and a
#: 200-player one is not a scroll.
MIN_PLOT_HEIGHT = 300
MAX_PLOT_HEIGHT = 620


@dataclass(frozen=True)
class ChartSeries:
    """One player's line."""

    user_id: int
    name: str
    #: 1-5 for a coloured series, None for the greys.
    slot: int | None
    #: True for the unnamed rest of the field: drawn, but not labelled and not
    #: in the tooltip.
    is_field: bool
    #: The `points` attribute of the polyline, already in SVG coordinates.
    polyline: str
    label_x: float
    label_y: float
    final_rank: int
    best_rank: int
    worst_rank: int
    points: int
    picks: int
    correct: int
    hit_rate: int
    #: [index, rank, points] per scored match, for the hover layer.
    samples: list[list[int]] = field(default_factory=list)

    @property
    def css_class(self) -> str:
        if self.slot:
            return f"series-{self.slot}"
        return "series-field" if self.is_field else "series-context"


@dataclass(frozen=True)
class RankChart:
    #: Unique per pool, so two charts on one page do not share a script tag.
    dom_id: str
    width: int
    height: int
    plot_left: float
    plot_right: float
    plot_top: float
    plot_bottom: float
    series: list[ChartSeries]
    #: (y, rank) for the horizontal guides.
    y_ticks: list[tuple[float, int]]
    #: (x, label) for the date axis.
    x_ticks: list[tuple[float, str]]
    #: One label per scored match, indexed 0-based, for the tooltip.
    match_labels: list[str]
    #: [names, points] of whoever held rank 1 after each match.
    leaders: list[list]
    entry_count: int
    max_rank: int
    total_players: int

    @property
    def plot_width(self) -> float:
        return self.plot_right - self.plot_left

    @property
    def plot_height(self) -> float:
        return self.plot_bottom - self.plot_top

    @property
    def has_context_series(self) -> bool:
        return any(s.slot is None and not s.is_field for s in self.series)

    @property
    def has_field_series(self) -> bool:
        return any(s.is_field for s in self.series)

    @property
    def coloured(self) -> list[ChartSeries]:
        return [s for s in self.series if s.slot]

    @property
    def named(self) -> list[ChartSeries]:
        """The series that get an end-of-line label and a tooltip row."""
        return [s for s in self.series if not s.is_field]

    @property
    def field(self) -> list[ChartSeries]:
        return [s for s in self.series if s.is_field]

    @property
    def payload(self) -> dict:
        """What the hover layer needs, rendered through Django's json_script.

        Geometry is deliberately absent - the script reads the plot box off the
        SVG's own attributes, so there is one source of truth for where the
        chart is and the two cannot drift apart.
        """
        return {
            "matches": self.match_labels,
            "entries": self.entry_count,
            "maxRank": self.max_rank,
            # The reader can change who is drawn, so *every* player ships their
            # samples, not just the named ten - the alternative is a request
            # per checkbox for data the page already rendered as a polyline.
            "highlighted": CHART_HIGHLIGHTED,
            "labelGap": LABEL_MIN_GAP,
            "series": [
                {
                    "id": s.user_id,
                    "name": s.name,
                    "cls": s.css_class,
                    "rank": s.final_rank,
                    "shown": not s.is_field,
                    "samples": s.samples,
                }
                for s in self.series
            ],
            # Whoever actually led after each match. The named ten are the
            # *final* top ten, so early on the leader is often not among them -
            # without this the tooltip would open at 4th and leave the reader
            # asking who was first.
            "leaders": self.leaders,
        }


def _rank_tick_step(max_rank: int) -> int:
    """A tick every 1, 2, 5, 10 ... ranks, whichever gives about five guides."""
    for step in (1, 2, 5, 10, 20, 50, 100):
        if max_rank / step <= 6:
            return step
    return 100


def _spread_labels(labels: list[tuple[float, ChartSeries]]) -> dict[int, float]:
    """Nudge end-of-line labels apart, keeping their order.

    Players level on the final rank share a y, so without this their names
    print on top of one another. One downward pass is enough: the list is
    already sorted, and each label only ever moves away from the one above it.
    """
    placed: dict[int, float] = {}
    previous_y = None
    for y, series in labels:
        if previous_y is not None and y - previous_y < LABEL_MIN_GAP:
            y = previous_y + LABEL_MIN_GAP
        placed[series.user_id] = y
        previous_y = y
    return placed


def build_rank_chart(
    history: list[HistoryEntry],
    names: dict[int, str],
    dom_id: str = "rank-chart",
) -> RankChart | None:
    """Lay out a pool's rank history as an SVG-ready chart.

    The leading `CHART_SERIES` players by final rank are named - labelled at
    the end of their line and listed in the tooltip - and the first
    `CHART_HIGHLIGHTED` of those carry a colour.

    **The rest of the field is still drawn**, as a faint band behind them. It
    is not decoration: the named ten are the *final* top ten, so at any earlier
    match the player leading might have finished 30th, and drawing only the ten
    left visible holes where nothing sat on rank 1 - a reader cannot tell that
    from a bug. With the whole field drawn, the top edge of the band is the
    leader at every match, and the tooltip names them.

    Returns None below two scored matches: one point is not a line, and a chart
    of it would be an axis with a dot on it.
    """
    if len(history) < 2:
        return None

    final = history[-1]
    leaders: list[PlayerStanding] = sorted(final.standings.values(), key=lambda s: (s.rank, names.get(s.user_id, "")))
    # Named first, then the rest of the field in final order, so the drawing
    # order below puts the band underneath.
    charted = leaders

    # Every rank anyone held, because the band covers the whole field.
    max_rank = max(standing.rank for entry in history for standing in entry.standings.values())
    max_rank = max(max_rank, 2)

    plot_height = min(max((max_rank - 1) * ROW_HEIGHT, MIN_PLOT_HEIGHT), MAX_PLOT_HEIGHT)
    height = plot_height + PAD_TOP + PAD_BOTTOM

    plot_left = PAD_LEFT
    plot_right = WIDTH - PAD_RIGHT
    plot_top = PAD_TOP
    plot_bottom = height - PAD_BOTTOM
    plot_width = plot_right - plot_left

    def x_for(index: int) -> float:
        return plot_left + (index - 1) / (len(history) - 1) * plot_width

    def y_for(rank: int) -> float:
        return plot_top + (rank - 1) / (max_rank - 1) * plot_height

    series: list[ChartSeries] = []
    label_seeds: list[tuple[float, ChartSeries]] = []

    for position, standing in enumerate(charted):
        user_id = standing.user_id
        is_field = position >= CHART_SERIES
        ranks = [(entry.index, entry.standings[user_id]) for entry in history if user_id in entry.standings]
        polyline = " ".join(f"{x_for(index):.1f},{y_for(point.rank):.1f}" for index, point in ranks)

        entry = ChartSeries(
            user_id=user_id,
            name=names.get(user_id, f"Player {user_id}"),
            slot=position + 1 if position < CHART_HIGHLIGHTED else None,
            is_field=is_field,
            polyline=polyline,
            label_x=plot_right + 8,
            label_y=y_for(standing.rank),
            final_rank=standing.rank,
            best_rank=min(point.rank for _index, point in ranks),
            worst_rank=max(point.rank for _index, point in ranks),
            points=standing.points,
            picks=standing.picks,
            correct=standing.correct,
            hit_rate=standing.hit_rate,
            samples=[[index, point.rank, point.points] for index, point in ranks],
        )
        series.append(entry)
        if not is_field:
            label_seeds.append((entry.label_y, entry))

    label_ys = _spread_labels(sorted(label_seeds, key=lambda pair: pair[0]))
    series = [ChartSeries(**{**s.__dict__, "label_y": label_ys.get(s.user_id, s.label_y)}) for s in series]

    step = _rank_tick_step(max_rank)
    y_ticks = [(y_for(rank), rank) for rank in range(1, max_rank + 1) if rank == 1 or rank % step == 0]

    # Roughly six date ticks, and never two close enough to print over each
    # other - the last match usually lands near the previous tick.
    x_ticks: list[tuple[float, str]] = []
    tick_every = max(1, round(len(history) / 6))
    for entry in history:
        if not (entry.index == 1 or entry.index == len(history) or entry.index % tick_every == 0):
            continue
        x = x_for(entry.index)
        local: datetime.datetime = timezone.localtime(entry.match.kickoff)
        label = local.strftime("%-d %b")

        if x_ticks and x - x_ticks[-1][0] < X_TICK_MIN_GAP:
            # The final match wins the slot: the end of the season is the more
            # useful label of the two.
            if entry.index == len(history):
                x_ticks.pop()
            else:
                continue
        # Several matches on one day is normal - a World Cup group stage plays
        # four - and printing the date once per tick just repeats it.
        if x_ticks and x_ticks[-1][1] == label:
            continue

        x_ticks.append((x, label))

    chart_leaders = []
    for entry in history:
        leading = sorted(
            (standing for standing in entry.standings.values() if standing.rank == 1),
            key=lambda standing: names.get(standing.user_id, ""),
        )
        # A tie for the lead is rare but real; two names is as many as the
        # tooltip row can carry before it stops being one line.
        shown = [names.get(standing.user_id, "?") for standing in leading[:2]]
        if len(leading) > 2:
            shown.append(f"+{len(leading) - 2}")
        chart_leaders.append([" & ".join(shown), leading[0].points if leading else 0])

    return RankChart(
        dom_id=dom_id,
        width=WIDTH,
        height=height,
        plot_left=plot_left,
        plot_right=plot_right,
        plot_top=plot_top,
        plot_bottom=plot_bottom,
        series=series,
        y_ticks=y_ticks,
        x_ticks=x_ticks,
        match_labels=[f"{entry.match.home_team.name} v {entry.match.away_team.name}" for entry in history],
        leaders=chart_leaders,
        entry_count=len(history),
        max_rank=max_rank,
        total_players=len(final.standings),
    )
