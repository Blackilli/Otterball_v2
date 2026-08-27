/* Hover layer for the rank-over-time chart.
 *
 * The chart itself is server-rendered SVG - lines, axes and end-of-line player
 * labels are all in the markup. This adds the crosshair and tooltip on top, so
 * a reader can put a number on a crossing instead of estimating it. With
 * script off, the chart is unchanged and still readable.
 *
 * Geometry is read off the SVG rather than duplicated here: the hover target
 * rect already carries the plot box the server laid out, so there is one
 * source of truth for where the plot is.
 */

/* 1st, 2nd, 3rd, 4th - and 11th/12th/13th, which are the ones a naive
 * last-digit rule gets wrong. */
function ordinal(rank) {
    const teens = rank % 100;
    if (teens >= 11 && teens <= 13) return `${rank}th`;
    return `${rank}${["th", "st", "nd", "rd"][rank % 10] || "th"}`;
}

/* `hidden` is an HTMLElement property; assigning it on an SVG element sets a
 * useless expando and leaves the attribute - and the group - exactly as it
 * was. The attribute has to be moved by hand. */
function setHidden(element, isHidden) {
    if (isHidden) element.setAttribute("hidden", "");
    else element.removeAttribute("hidden");
}

/* Player names come from Discord display names, which are user-controlled, and
 * the tooltip is built with innerHTML. Escape everything that goes into it. */
function escapeHtml(value) {
    return String(value).replace(
        /[&<>"']/g,
        (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
    );
}

function setUp(figure) {
    const svg = figure.querySelector("svg");
    const target = figure.querySelector(".hover-target");
    const crosshair = figure.querySelector(".crosshair");
    const crosshairLine = crosshair?.querySelector("line");
    const tooltip = figure.querySelector(".chart-tooltip");
    const payloadEl = document.getElementById(figure.dataset.chartId);
    if (!svg || !target || !crosshair || !tooltip || !payloadEl) return;

    const data = JSON.parse(payloadEl.textContent);
    const left = parseFloat(target.getAttribute("x"));
    const width = parseFloat(target.getAttribute("width"));
    const top = parseFloat(target.getAttribute("y"));
    const height = parseFloat(target.getAttribute("height"));

    // Rank -> y, the same mapping sports/charts.py used to place the lines.
    const yFor = (rank) => top + ((rank - 1) / (data.maxRank - 1)) * height;

    // One lookup per series so the tooltip does not scan every sample on move.
    const byIndex = data.series.map((series) => {
        const map = new Map();
        for (const [index, rank, points] of series.samples) map.set(index, { rank, points });
        return { name: series.name, cls: series.cls, map };
    });

    const dots = data.series.map((series) => {
        const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        dot.setAttribute("r", "5");
        dot.setAttribute("class", `hover-dot ${series.cls}`);
        dot.setAttribute("visibility", "hidden");
        crosshair.appendChild(dot);
        return dot;
    });

    function indexAt(clientX) {
        const box = svg.getBoundingClientRect();
        // The SVG scales to its container, so client pixels have to come back
        // through the viewBox before they mean anything in plot coordinates.
        const viewX = ((clientX - box.left) / box.width) * svg.viewBox.baseVal.width;
        const ratio = (viewX - left) / width;
        const index = Math.round(ratio * (data.entries - 1)) + 1;
        return Math.min(Math.max(index, 1), data.entries);
    }

    function show(clientX) {
        const index = indexAt(clientX);
        const x = left + ((index - 1) / (data.entries - 1)) * width;

        setHidden(crosshair, false);
        crosshairLine.setAttribute("x1", x);
        crosshairLine.setAttribute("x2", x);

        const rows = [];
        byIndex.forEach((series, i) => {
            const point = series.map.get(index);
            if (!point) {
                dots[i].setAttribute("visibility", "hidden");
                return;
            }
            dots[i].setAttribute("visibility", "visible");
            dots[i].setAttribute("cx", x);
            dots[i].setAttribute("cy", yFor(point.rank));
            rows.push({ ...series, ...point });
        });
        rows.sort((a, b) => a.rank - b.rank);

        const line = (cls, name, rank, points, extra = "") =>
            `<li class="${extra}"><span class="swatch ${cls}"></span>` +
            `<span class="tooltip-name">${escapeHtml(name)}</span>` +
            `<span class="tooltip-rank">${rank}</span>` +
            `<span class="tooltip-points">${points}</span></li>`;

        // The named series are the *final* top ten, so the leader at this
        // match may not be among them. Open on them either way, or the
        // tooltip starts at 4th with no explanation.
        const [leaderNames, leaderPoints] = data.leaders[index - 1];
        const leaderRow = rows.some((row) => row.rank === 1)
            ? ""
            : line("series-field", leaderNames, "1st", leaderPoints, "tooltip-leader");

        tooltip.hidden = false;
        tooltip.innerHTML =
            `<p class="tooltip-head">${escapeHtml(data.matches[index - 1])}</p>` +
            `<p class="tooltip-sub">after match ${index} of ${data.entries}</p>` +
            `<ul>${leaderRow}${rows
                .map((row) => line(row.cls, row.name, ordinal(row.rank), row.points))
                .join("")}</ul>`;

        // Flip to the other side near the right edge so the tooltip never
        // leaves the frame it is anchored in.
        const frame = figure.querySelector(".chart-frame").getBoundingClientRect();
        const offset = clientX - frame.left;
        const flip = offset > frame.width - tooltip.offsetWidth - 24;
        tooltip.style.left = `${flip ? offset - tooltip.offsetWidth - 16 : offset + 16}px`;
    }

    function hide() {
        setHidden(crosshair, true);
        tooltip.hidden = true;
        dots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
    }

    target.addEventListener("pointermove", (event) => show(event.clientX));
    target.addEventListener("pointerleave", hide);
    // Touch: a tap reads as a move, and the tooltip stays until the next tap
    // elsewhere, since there is no hover to leave.
    target.addEventListener("pointerdown", (event) => show(event.clientX));
}

document.querySelectorAll("[data-rank-chart]").forEach(setUp);
