/* Guild-scoped pickers on the pool setup page.
 *
 * The channel and role lists hold every channel and role the bot has ever
 * seen, across every server. Picking a guild narrows them to that guild's,
 * which is the difference between a usable dropdown and scrolling 59 channels
 * looking for the guild prefix.
 *
 * This is a convenience only. The options carry their guild in `data-guild`
 * and the server re-checks the pairing in PoolSetupForm.clean, so with
 * scripting off the page still works and still cannot save a channel from the
 * wrong server.
 */

const SCOPED_FIELDS = ["id_channel", "id_notification_role"];
const NO_GUILD_CHOSEN = "— pick a guild first —";

/* Options are labelled "Guild · name" so the unfiltered list is readable. Once
 * the list *is* filtered, the guild half is just noise on every row. */
function withoutGuildPrefix(label) {
    const parts = label.split(" · ");
    return parts.length > 1 ? parts.slice(1).join(" · ") : label;
}

function setUp() {
    const guildSelect = document.getElementById("id_guild");
    const scoped = SCOPED_FIELDS.map((id) => document.getElementById(id)).filter(Boolean);
    if (!guildSelect || !scoped.length) return;

    // Snapshot every option once. The selects are rebuilt from this on each
    // change, rather than hidden and unhidden - `hidden` on an <option> is not
    // honoured everywhere, and a hidden-but-selectable option is worse than no
    // filtering at all.
    const catalogue = new Map(
        scoped.map((select) => [
            select,
            Array.from(select.options).map((option) => ({
                value: option.value,
                label: option.textContent.trim(),
                guild: option.dataset.guild || "",
                selected: option.selected,
            })),
        ]),
    );

    function refresh() {
        const chosenGuild = guildSelect.value;

        for (const select of scoped) {
            const options = catalogue.get(select);
            const previous = select.value;
            select.replaceChildren();

            if (!chosenGuild) {
                select.append(new Option(NO_GUILD_CHOSEN, ""));
                select.disabled = true;
                continue;
            }

            select.disabled = false;
            for (const option of options) {
                // The blank choice carries no guild and has to survive the
                // filter: both of these fields are optional.
                if (option.guild && option.guild !== chosenGuild) continue;
                select.append(new Option(option.guild ? withoutGuildPrefix(option.label) : option.label, option.value));
            }

            // Keep the choice if it belongs to the new guild, drop it if not.
            const stillValid = Array.from(select.options).some((option) => option.value === previous);
            select.value = stillValid ? previous : "";
        }
    }

    guildSelect.addEventListener("change", refresh);
    // Run once on load, so a form redisplayed with errors comes back filtered.
    refresh();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setUp);
} else {
    setUp();
}
