/* Hover layer and player picker for the rank-over-time chart.
 *
 * The chart itself is server-rendered SVG - lines, axes and end-of-line player
 * labels are all in the markup. This adds the crosshair, the tooltip and the
 * "which players are drawn" controls on top, so a reader can put a number on a
 * crossing instead of estimating it. With script off, the chart keeps the ten
 * the server chose and is still readable; the picker never appears, because
 * its checkboxes submit nothing.
 *
 * Geometry is read off the SVG rather than duplicated here: the hover target
 * rect already carries the plot box the server laid out, so there is one
 * source of truth for where the plot is.
 */

const FIELD_CLASS = "series-field";
const CONTEXT_CLASS = "series-context";

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

    const lineGroup = figure.querySelector(".lines");
    const lines = new Map();
    const labels = new Map();
    figure.querySelectorAll(".lines polyline[data-user]").forEach((el) => lines.set(Number(el.dataset.user), el));
    figure.querySelectorAll(".line-labels text[data-user]").forEach((el) => labels.set(Number(el.dataset.user), el));

    // The table view under the chart carries the same swatches and must agree
    // with it about who is which colour, so the scope is the whole board.
    const swatchScope = figure.closest("section") || document;

    // One lookup per series so the tooltip does not scan every sample on move.
    const byId = new Map(
        data.series.map((series) => {
            const map = new Map();
            for (const [index, rank, points] of series.samples) map.set(index, { rank, points });
            return [series.id, map];
        }),
    );

    const dots = new Map(
        data.series.map((series) => {
            const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            dot.setAttribute("r", "5");
            dot.setAttribute("visibility", "hidden");
            crosshair.appendChild(dot);
            return [series.id, dot];
        }),
    );

    const selected = new Set(data.series.filter((series) => series.shown).map((series) => series.id));
    // Rebuilt on every selection change: the colour a player carries depends
    // on where they land in the *current* selection, not on their final rank.
    let drawn = [];

    /* Nudge the visible labels apart, keeping their order - players level on a
     * rank share a y and would otherwise print on top of each other. Select
     * everyone in a big pool and the wanted gap no longer fits, so it shrinks
     * to whatever the plot has room for rather than running off the bottom. */
    function placeLabels(entries) {
        if (!entries.length) return;
        const gap = entries.length > 1 ? Math.min(data.labelGap, height / (entries.length - 1)) : data.labelGap;

        let previous = null;
        for (const entry of entries) {
            entry.y = previous !== null && entry.y - previous < gap ? previous + gap : entry.y;
            previous = entry.y;
        }
        // A downward pass can push the last label past the plot; pulling the
        // stack back up is what keeps the bottom names inside the frame.
        const overflow = entries[entries.length - 1].y - (top + height);
        if (overflow <= 0) return;
        previous = null;
        for (let i = entries.length - 1; i >= 0; i -= 1) {
            let y = entries[i].y - overflow;
            if (previous !== null && previous - y < gap) y = previous - gap;
            entries[i].y = Math.max(y, top);
            previous = entries[i].y;
        }
    }

    function renderLegend() {
        const legend = figure.querySelector("[data-chart-legend]");
        if (!legend) return;
        const coloured = drawn.filter((series) => series.cls.startsWith("series-") && series.cls !== CONTEXT_CLASS);
        const rows = coloured.map(
            (series) =>
                `<li><span class="swatch ${series.cls}"></span>${escapeHtml(series.name.slice(0, 20))}</li>`,
        );
        if (drawn.length > coloured.length) {
            rows.push(`<li><span class="swatch ${CONTEXT_CLASS}"></span>${drawn.length - coloured.length} more selected</li>`);
        }
        if (drawn.length < data.series.length) {
            rows.push(`<li><span class="swatch ${FIELD_CLASS}"></span>rest of the field</li>`);
        }
        legend.innerHTML = rows.join("");
    }

    function render() {
        drawn = data.series.filter((series) => selected.has(series.id));
        const classFor = (position) => (position < data.highlighted ? `series-${position + 1}` : CONTEXT_CLASS);
        const classes = new Map(data.series.map((series) => [series.id, FIELD_CLASS]));
        drawn.forEach((series, position) => {
            series.cls = classFor(position);
            classes.set(series.id, series.cls);
        });

        for (const [id, cls] of classes) {
            const line = lines.get(id);
            if (line) line.setAttribute("class", `line ${cls}`);
            const label = labels.get(id);
            if (label) {
                label.setAttribute("class", `line-label ${cls}`);
                setHidden(label, !selected.has(id));
            }
            const dot = dots.get(id);
            if (dot) dot.setAttribute("class", `hover-dot ${cls}`);
            swatchScope
                .querySelectorAll(`.swatch[data-user="${id}"]`)
                .forEach((swatch) => swatch.setAttribute("class", `swatch ${cls}`));
        }

        // Document order is paint order in SVG: a selected line has to be
        // appended after the field, or it is drawn underneath it.
        if (lineGroup) {
            for (const series of drawn) {
                const line = lines.get(series.id);
                if (line) lineGroup.appendChild(line);
            }
        }

        const placed = drawn
            .map((series) => ({ id: series.id, y: yFor(series.rank) }))
            .sort((a, b) => a.y - b.y);
        placeLabels(placed);
        for (const entry of placed) labels.get(entry.id)?.setAttribute("y", entry.y.toFixed(1));

        const caption = figure.querySelector("[data-chart-caption]");
        if (caption) {
            caption.textContent = drawn.length
                ? `${drawn.length} of ${data.series.length} players are named;`
                : `No players are named right now;`;
        }
        renderLegend();
        hide();
    }

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
        dots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
        for (const series of drawn) {
            const point = byId.get(series.id)?.get(index);
            if (!point) continue;
            const dot = dots.get(series.id);
            dot.setAttribute("visibility", "visible");
            dot.setAttribute("cx", x);
            dot.setAttribute("cy", yFor(point.rank));
            rows.push({ ...series, ...point });
        }
        rows.sort((a, b) => a.rank - b.rank);

        const line = (cls, name, rank, points, extra = "") =>
            `<li class="${extra}"><span class="swatch ${cls}"></span>` +
            `<span class="tooltip-name">${escapeHtml(name)}</span>` +
            `<span class="tooltip-rank">${rank}</span>` +
            `<span class="tooltip-points">${points}</span></li>`;

        // The selected players need not include whoever led at this match -
        // by default they are the *final* top ten. Open on the leader either
        // way, or the tooltip starts at 4th with no explanation.
        const [leaderNames, leaderPoints] = data.leaders[index - 1];
        const leaderRow = rows.some((row) => row.rank === 1)
            ? ""
            : line(FIELD_CLASS, leaderNames, "1st", leaderPoints, "tooltip-leader");

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

    const picker = figure.querySelector("[data-chart-picker]");
    if (picker) {
        const boxes = [...picker.querySelectorAll("input[type=checkbox]")];
        picker.addEventListener("change", (event) => {
            const box = event.target;
            if (!box.matches("input[type=checkbox]")) return;
            const id = Number(box.value);
            if (box.checked) selected.add(id);
            else selected.delete(id);
            render();
        });
        const setAll = (checked) => {
            for (const box of boxes) {
                box.checked = checked;
                if (checked) selected.add(Number(box.value));
                else selected.delete(Number(box.value));
            }
            render();
        };
        picker.querySelector("[data-select-all]")?.addEventListener("click", () => setAll(true));
        picker.querySelector("[data-select-none]")?.addEventListener("click", () => setAll(false));
        setHidden(picker, false);
    }

    render();
}

document.querySelectorAll("[data-rank-chart]").forEach(setUp);
