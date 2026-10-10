#!/usr/bin/env python3
import asyncio
import io
import json
import logging
import os
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import discord
from discord import app_commands, ui

import tvpresence as tv

TOKEN_FILE = tv.CONFIG_DIR / "bot_token"
PANEL_FILE = tv.CONFIG_DIR / "panel.json"
TVCAST = Path.home() / ".local" / "bin" / "tvcast"
TV_NAME = tv.setting("tv_name", "Télé")
STREAM_KINDS = {"movie", "episode"}
OPEN_TV_WINDOW = False
SCREENSHOT_TTL = 120
RED, GREY = 0xE50914, 0x2B2D31

KEYS = {
    "prev": ("⏮️", "MEDIA_PREVIOUS", None), "rew": ("⏪", "MEDIA_REWIND", None),
    "pp": ("⏯️", "MEDIA_PLAY_PAUSE", "primary"), "ff": ("⏩", "MEDIA_FAST_FORWARD", None),
    "next": ("⏭️", "MEDIA_NEXT", None),
    "back": ("↩️", "BACK", None), "up": ("🔼", "DPAD_UP", None), "home": ("🏠", "HOME", None),
    "vdown": ("🔉", "VOLUME_DOWN", None), "vup": ("🔊", "VOLUME_UP", None),
    "left": ("◀️", "DPAD_LEFT", None), "ok": ("🆗", "DPAD_CENTER", "primary"),
    "right": ("▶️", "DPAD_RIGHT", None), "mute": ("🔇", "VOLUME_MUTE", None),
    "power": ("📴", "POWER", "danger"), "down": ("🔽", "DPAD_DOWN", None),
}
ROWS = [
    ["prev", "rew", "pp", "ff", "next"],
    ["back", "up", "home", "vdown", "vup"],
    ["left", "ok", "right", "mute", "power"],
    ["capture", "down", "cast", "type", "refresh"],
]
EXTRA = {"capture": ("📸", "Capture"), "cast": ("🔗", "Caster"), "type": ("⌨️", "Texte"),
         "refresh": ("🔄", None)}
STYLES = {None: discord.ButtonStyle.secondary, "primary": discord.ButtonStyle.primary,
          "danger": discord.ButtonStyle.danger, "success": discord.ButtonStyle.success}


def webhook_target():
    try:
        req = urllib.request.Request(tv.WEBHOOK_FILE.read_text().strip(), headers=tv.UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        return int(d["guild_id"]), int(d["channel_id"])
    except Exception:
        return None, None


def serial():
    return tv.STATE.get("serial") or tv.adb_serial()


def press(keycode):
    tv.adb(serial(), "input", "keyevent", f"KEYCODE_{keycode}")


def type_text(text):
    tv.adb(serial(), "input", "text", shlex.quote(text.replace(" ", "%s")))


def launch_app(pkg):
    tv.adb(serial(), "monkey", "-p", pkg, "-c", "android.intent.category.LEANBACK_LAUNCHER", "1")


def installed_apps():
    out = tv.adb(serial(), "pm", "list", "packages")
    pkgs = {line.removeprefix("package:").strip() for line in out.splitlines()}
    seen, apps = set(), []
    for pkg, (name, _) in tv.APPS.items():
        if pkg in pkgs and name not in seen and pkg != "com.google.android.apps.mediashell":
            seen.add(name)
            apps.append((pkg, "SmartTube" if pkg == "org.smarttube.stable" else name))
    return apps


def screenshot():
    png = subprocess.run(["adb", "-s", serial(), "exec-out", "screencap", "-p"],
                         capture_output=True, timeout=20).stdout
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d, "tv.png"), Path(d, "tv.jpg")
        src.write_bytes(png)
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "70", "-Z", "1280",
                        str(src), "--out", str(dst)], capture_output=True, timeout=20)
        return dst.read_bytes()


def cast(url):
    r = subprocess.run([str(TVCAST), url], capture_output=True, text=True, timeout=90,
                       env={**os.environ, "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"})
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]


def notify(title, text):
    script = f"display notification {json.dumps(text)} with title {json.dumps(title)} sound name \"Glass\""
    subprocess.run(["osascript", "-e", script], capture_output=True)


def now_playing_md():
    act, info = tv.STATE.get("act"), tv.STATE.get("info")
    if not act:
        return "💤 **Rien en cours**\n-# Accueil, veille ou télé éteinte"
    main_line, second = tv.display(act, info)
    lines = [f"**{main_line}**"]
    if second:
        lines.append(f"*{second}*")
    if act["paused"]:
        status = "⏸️ En pause"
    elif act["progress"]:
        status = f"▶️ Fin <t:{act['progress'][1]}:R>"
    elif act["live"]:
        status = "🔴 En direct"
    else:
        status = f"▶️ Depuis <t:{tv.STATE['since']}:R>"
    lines.append(f"{act['app']} · {status}")
    return "\n".join(lines)


def thumbnail_url():
    act, info = tv.STATE.get("act"), tv.STATE.get("info")
    if info and info.get("poster"):
        return info["poster"]
    if act and tv.app_logo(act["pkg"]):
        return tv.app_logo(act["pkg"])
    return "https://www.google.com/s2/favicons?domain=tv.google&sz=256"


def build_panel(owner_id, apps, shot):
    playing = bool(tv.STATE.get("act"))
    children = [ui.Section(ui.TextDisplay(f"### 📺 Télécommande · {TV_NAME}"),
                           ui.TextDisplay(now_playing_md()),
                           accessory=ui.Thumbnail(thumbnail_url()))]
    if shot:
        children.append(ui.MediaGallery(discord.MediaGalleryItem("attachment://tv.jpg")))
    children.append(ui.Separator())
    for row in ROWS:
        buttons = []
        for key in row:
            if key in KEYS:
                emoji, _, style = KEYS[key]
                buttons.append(ui.Button(emoji=emoji, style=STYLES[style], custom_id=f"tv:key:{key}"))
            else:
                emoji, label = EXTRA[key]
                style = discord.ButtonStyle.success if key == "cast" else discord.ButtonStyle.secondary
                buttons.append(ui.Button(emoji=emoji, label=label, style=style, custom_id=f"tv:{key}"))
        children.append(ui.ActionRow(*buttons))
    if apps:
        children.append(ui.ActionRow(ui.Select(
            custom_id="tv:app", placeholder="🚀 Lancer une app…",
            options=[discord.SelectOption(label=name, value=pkg) for pkg, name in apps[:25]])))
    children.append(ui.TextDisplay(f"-# 🔒 Réservé à <@{owner_id}> · mis à jour <t:{int(time.time())}:R>"))
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*children, accent_colour=RED if playing else GREY))
    return view


class CastModal(ui.Modal, title="🔗 Caster sur la télé"):
    url = ui.TextInput(label="Lien de la vidéo ou de la page", placeholder="https://…")

    async def on_submit(self, interaction):
        await interaction.response.send_message("📡 Envoi sur la télé…", ephemeral=True)
        ok, last = await asyncio.to_thread(cast, self.url.value.strip())
        await interaction.edit_original_response(
            content=("✅ C'est parti sur la télé !" if ok else f"❌ Impossible de caster : `{last[0][:150]}`"))


class TypeModal(ui.Modal, title="⌨️ Taper sur la télé"):
    text = ui.TextInput(label="Texte (champ de recherche sélectionné sur la télé)", max_length=200)

    async def on_submit(self, interaction):
        await interaction.response.send_message("⌨️ Envoyé !", ephemeral=True)
        await asyncio.to_thread(type_text, self.text.value)


class TVBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.owner_id = None
        self.guild_id, self.channel_id = webhook_target()
        self.panel = None
        self.apps = []
        self.shot = None
        self.refresh_task = None
        self.stream_proc = None

    async def setup_hook(self):
        info = await self.application_info()
        self.owner_id = (info.team.owner_id if info.team else info.owner.id)
        tv.LISTENERS.append(self.on_tv_event)
        threading.Thread(target=tv.run, daemon=True, name="tvpresence").start()
        self.register_commands()
        await self.sync_commands()

    async def sync_commands(self):
        if not self.guild_id:
            return
        guild = discord.Object(self.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
            print("✓ Commandes /tv prêtes", flush=True)
        except discord.Forbidden:
            print("… Bot pas encore invité sur le serveur (commandes /tv en attente)", flush=True)

    async def on_guild_join(self, guild):
        if guild.id == self.guild_id:
            await self.sync_commands()

    async def on_ready(self):
        print(f"✓ Bot connecté : {self.user}", flush=True)
        self.apps = await asyncio.to_thread(installed_apps)
        try:
            saved = json.loads(PANEL_FILE.read_text())
            channel = self.get_channel(saved["channel_id"]) or await self.fetch_channel(saved["channel_id"])
            self.panel = await channel.fetch_message(saved["message_id"])
            await self.refresh_panel()
        except Exception:
            self.panel = None

    def register_commands(self):
        group = app_commands.Group(name="tv", description="Télécommande de la télé")

        @group.command(name="panneau", description="Affiche la télécommande dans ce salon")
        async def panneau(interaction: discord.Interaction):
            if not await self.check_owner(interaction):
                return
            await interaction.response.send_message("📺 Télécommande installée ici !", ephemeral=True)
            if self.panel:
                try:
                    await self.panel.delete()
                except discord.HTTPException:
                    pass
            try:
                self.panel = await interaction.channel.send(view=build_panel(self.owner_id, self.apps, None))
            except discord.HTTPException as e:
                print(f"… panneau : {e}", flush=True)
                return await interaction.edit_original_response(content=f"❌ Impossible d'envoyer le panneau : `{e}`")
            PANEL_FILE.write_text(json.dumps({"channel_id": self.panel.channel.id,
                                              "message_id": self.panel.id}))

        @group.command(name="cast", description="Envoie une vidéo ou une page sur la télé")
        @app_commands.describe(lien="Lien YouTube, Twitch, Vimeo, page web…")
        async def cast_cmd(interaction: discord.Interaction, lien: str):
            if not await self.check_owner(interaction):
                return
            await interaction.response.send_message("📡 Envoi sur la télé…", ephemeral=True)
            ok, last = await asyncio.to_thread(cast, lien)
            await interaction.edit_original_response(
                content="✅ C'est parti sur la télé !" if ok else f"❌ Impossible de caster : `{last[0][:150]}`")

        @group.command(name="capture", description="Capture de l'écran de la télé (visible par toi seul)")
        async def capture(interaction: discord.Interaction):
            if not await self.check_owner(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            jpg = await asyncio.to_thread(screenshot)
            await interaction.followup.send(file=discord.File(io.BytesIO(jpg), "tv.jpg"), ephemeral=True)

        self.tree.add_command(group)

    async def check_owner(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(f"🔒 Cette télécommande est réservée à <@{self.owner_id}>.",
                                                ephemeral=True)
        return False

    async def refresh_panel(self):
        if not self.panel:
            return
        shot = self.shot if self.shot and time.time() - self.shot[1] < SCREENSHOT_TTL else None
        files = [discord.File(io.BytesIO(shot[0]), "tv.jpg")] if shot else []
        try:
            await self.panel.edit(view=build_panel(self.owner_id, self.apps, shot), attachments=files)
        except discord.NotFound:
            self.panel = None
        except discord.HTTPException as e:
            print(f"… panneau : {e}", flush=True)

    def schedule_refresh(self, delay=1.5):
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()

        async def later():
            await asyncio.sleep(delay)
            await self.refresh_panel()
        self.refresh_task = asyncio.ensure_future(later())

    async def on_interaction(self, interaction: discord.Interaction):
        cid = (interaction.data or {}).get("custom_id", "")
        if interaction.type != discord.InteractionType.component or not cid.startswith("tv:"):
            return
        if not await self.check_owner(interaction):
            return
        action = cid.split(":")[1]
        if action == "cast":
            return await interaction.response.send_modal(CastModal())
        if action == "type":
            return await interaction.response.send_modal(TypeModal())
        await interaction.response.defer()
        if action == "key":
            await asyncio.to_thread(press, KEYS[cid.split(":")[2]][1])
            self.schedule_refresh(2.5)
        elif action == "app":
            await asyncio.to_thread(launch_app, interaction.data["values"][0])
            self.schedule_refresh(4)
        elif action == "capture":
            self.shot = (await asyncio.to_thread(screenshot), time.time())
            await self.refresh_panel()
            self.schedule_refresh(SCREENSHOT_TTL + 1)
        elif action == "refresh":
            await self.refresh_panel()

    def on_tv_event(self, event, act, info=None, **_):
        asyncio.run_coroutine_threadsafe(self.handle_tv_event(event, act, info), self.loop)

    async def handle_tv_event(self, event, act, info):
        if event == "update":
            self.schedule_refresh(0.5)
        elif event == "start" and info and info["kind"] in STREAM_KINDS:
            await self.start_screening(act, info)
        elif event == "end":
            self.stop_stream()

    def owner_voice(self):
        guild = self.get_guild(self.guild_id) if self.guild_id else None
        member = guild and guild.get_member(self.owner_id)
        return member.voice.channel if member and member.voice else None

    async def start_screening(self, act, info):
        main_line, second = tv.display(act, info)
        voice = self.owner_voice()

        def links(*more):
            return ui.ActionRow(
                ui.Button(label="Bande-annonce", emoji="🎬", url="https://www.youtube.com/results?search_query="
                          + urllib.parse.quote_plus(f"{tv.clean_title(act['title'])} bande annonce")),
                ui.Button(label="Fiche", emoji="ℹ️", url=info["url"]), *more)

        def card(header, body, extra=None):
            view = ui.LayoutView(timeout=None)
            items = [ui.Section(ui.TextDisplay(header), ui.TextDisplay(body),
                                accessory=ui.Thumbnail(info["poster"] or thumbnail_url()))]
            items += [ui.Separator(), extra or links()]
            view.add_item(ui.Container(*items, accent_colour=RED))
            return view

        title_md = f"**{main_line}**" + (f"\n*{second}*" if second else "")

        channel = self.get_channel(self.channel_id) if self.channel_id else None
        role = channel and discord.utils.find(lambda r: r.name.lower() in ("cinéma", "cinema"), channel.guild.roles)
        if info["kind"] == "movie" and channel and role:
            extra = links(*([ui.Button(label=f"Rejoindre {voice.name}", emoji="🔊", url=voice.jump_url)]
                            if voice else []))
            await channel.send(view=card(f"## 🍿 Soirée film", f"{role.mention} <@{self.owner_id}> lance\n{title_md}",
                                         extra),
                               allowed_mentions=discord.AllowedMentions(roles=[role]))

        if voice:
            await voice.send(view=card("## 🍿 Séance en cours",
                                       f"<@{self.owner_id}> lance\n{title_md}"
                                       + ("\n-# Le stream arrive ici 👀" if OPEN_TV_WINDOW else "")))
            if OPEN_TV_WINDOW:
                self.start_stream()
                await asyncio.to_thread(notify, f"🍿 {main_line}",
                                        "Dans la voc : Partager l'écran → fenêtre « Télé · stream »")
                subprocess.run(["open", "-a", "Discord"])

    def start_stream(self):
        if self.stream_proc and self.stream_proc.poll() is None:
            return
        self.stream_proc = subprocess.Popen(
            ["scrcpy", "-s", serial(), "--window-title", "Télé · stream", "--audio-dup",
             "--max-size", "1920", "--video-bit-rate", "8M", "--no-control"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_stream(self):
        if self.stream_proc and self.stream_proc.poll() is None:
            self.stream_proc.terminate()
        self.stream_proc = None

    async def on_voice_state_update(self, member, before, after):
        if member.id == self.owner_id and before.channel and not after.channel:
            self.stop_stream()


def main():
    token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
    if not token:
        print("Pas de token de bot : statut Discord seulement.", flush=True)
        tv.run()
        return
    discord.utils.setup_logging(level=logging.WARNING)
    TVBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
