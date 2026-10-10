#!/usr/bin/env python3
import collections
import sys
import time

import tvpresence as tv

DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MEDALS = ["🥇", "🥈", "🥉"]


def recap(days=7):
    since = time.time() - days * 86400
    ends = [e for e in tv._history() if e.get("type") == "end" and e["ts"] >= since and e.get("watched", 0) >= 60]
    if not ends:
        return None

    total = sum(e["watched"] for e in ends)
    by_title = collections.Counter()
    by_app = collections.Counter()
    by_day = collections.Counter()
    kinds = {}
    for e in ends:
        by_title[e["title"]] += e["watched"]
        by_app[e["app"]] += e["watched"]
        by_day[time.strftime("%Y-%m-%d", time.localtime(e["ts"]))] += e["watched"]
        kinds[e["title"]] = e.get("kind")

    films = sum(1 for k in kinds.values() if k == "movie")
    episodes = sum(1 for k in kinds.values() if k == "episode")
    top = by_title.most_common(3)
    best_day, best_secs = by_day.most_common(1)[0]
    best_day_name = DAYS[time.strptime(best_day, "%Y-%m-%d").tm_wday]
    app, app_secs = by_app.most_common(1)[0]

    lines = [f"⏱️ **{tv.fmt_duration(total)}** devant la télé",
             f"🎬 {films} film{'s' * (films > 1)} · 📺 {episodes} épisode{'s' * (episodes > 1)}", ""]
    lines += ["**Top de la semaine**"]
    lines += [f"{MEDALS[i]} {tv.clean_title(t) if kinds[t] == 'episode' else t} — {tv.fmt_duration(s)}"
              for i, (t, s) in enumerate(top)]
    lines += ["", f"📱 App n°1 : **{app}** ({round(100 * app_secs / total)} %)",
              f"🔥 Grosse soirée : **{best_day_name}** ({tv.fmt_duration(best_secs)})"]

    embed = {"title": "📊 Ton récap télé de la semaine", "description": "\n".join(lines),
             "color": 0x5865F2, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    first = top[0][0]
    first_app = next(e["app"] for e in ends if e["title"] == first)
    pkg = next((p for p, (name, _) in tv.APPS.items() if name == first_app), "")
    info = tv.lookup_info({"pkg": pkg, "app": first_app, "title": first, "subtitle": "", "live": False})
    if info and info["poster"]:
        embed["thumbnail"] = {"url": info["poster"]}
    return embed


if __name__ == "__main__":
    embed = recap()
    if not embed:
        print("Rien regardé cette semaine, pas de récap.")
        sys.exit(0)
    if "--dry-run" in sys.argv:
        print(embed["description"])
    else:
        tv._webhook("POST", "", {"username": "Télé", "embeds": [embed]})
        print("Récap posté.")
