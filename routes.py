"""Arrangement Editor plugin — backend routes."""

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.dom import minidom

import base64

from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse


def setup(app, context):
    config_dir = context["config_dir"]
    get_dlc_dir = context["get_dlc_dir"]

    from lib.song import load_song
    from lib.psarc import unpack_psarc
    from lib.patcher import pack_psarc
    from lib.audio import find_wem_files, convert_wem

    STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"

    # Active editing sessions: session_id -> {dir, audio_file, filename, song_data}
    sessions = {}

    # ── List available CDLC files ────────────────────────────────────────

    @app.get("/api/plugins/editor/songs")
    async def list_songs():
        dlc_dir = get_dlc_dir()
        if not dlc_dir or not dlc_dir.exists():
            return []
        files = sorted(
            f.name for f in dlc_dir.iterdir()
            if f.suffix == ".psarc" and f.is_file()
        )
        return files

    # ── Load a CDLC for editing ──────────────────────────────────────────

    @app.post("/api/plugins/editor/load")
    async def load_cdlc(data: dict):
        filename = data.get("filename", "")
        if not filename:
            return JSONResponse({"error": "No filename"}, 400)

        dlc_dir = get_dlc_dir()
        filepath = dlc_dir / filename
        if not filepath.exists():
            return JSONResponse({"error": "File not found"}, 404)

        def _load():
            tmp_dir = tempfile.mkdtemp(prefix="slopsmith_editor_")
            try:
                unpack_psarc(str(filepath), tmp_dir)
                song = load_song(tmp_dir)
            except Exception as e:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise RuntimeError(f"Failed to load: {e}")

            # Convert audio
            audio_url = None
            audio_file = None
            wem_files = find_wem_files(tmp_dir)
            if wem_files:
                try:
                    audio_path = convert_wem(
                        wem_files[0], os.path.join(tmp_dir, "audio")
                    )
                    audio_file = audio_path
                    audio_id = Path(filename).stem.replace(" ", "_")
                    ext = Path(audio_path).suffix
                    dest = STATIC_DIR / f"editor_audio_{audio_id}{ext}"
                    shutil.copy2(audio_path, dest)
                    audio_url = f"/static/editor_audio_{audio_id}{ext}"
                except Exception as e:
                    print(f"[Editor] Audio conversion failed: {e}")

            # Find the arrangement XML files for later save
            xml_files = []
            for xf in Path(tmp_dir).rglob("*.xml"):
                try:
                    root = ET.parse(xf).getroot()
                    if root.tag == "song":
                        el = root.find("arrangement")
                        if el is not None and el.text:
                            low = el.text.lower().strip()
                            if low not in ("vocals", "showlights", "jvocals"):
                                xml_files.append(str(xf))
                except Exception:
                    continue

            result = _song_to_dict(song, audio_url)
            return result, tmp_dir, audio_file, xml_files

        try:
            result, session_dir, audio_file, xml_files = (
                await asyncio.get_event_loop().run_in_executor(None, _load)
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

        session_id = Path(filename).stem
        # Clean up previous session for same file
        if session_id in sessions:
            old = sessions[session_id]
            shutil.rmtree(old["dir"], ignore_errors=True)

        sessions[session_id] = {
            "dir": session_dir,
            "audio_file": audio_file,
            "filename": filename,
            "xml_files": xml_files,
        }
        result["session_id"] = session_id
        return result

    # ── Save edited arrangement back to PSARC ────────────────────────────

    @app.post("/api/plugins/editor/save")
    async def save_cdlc(data: dict):
        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "No active session"}, 400)

        arrangement_index = data.get("arrangement_index", 0)
        notes = data.get("notes", [])
        chords = data.get("chords", [])
        chord_templates = data.get("chord_templates", [])
        beats = data.get("beats", [])
        sections = data.get("sections", [])
        metadata = data.get("metadata", {})

        def _save():
            xml_files = session["xml_files"]
            if arrangement_index >= len(xml_files):
                raise RuntimeError("Invalid arrangement index")

            xml_path = xml_files[arrangement_index]

            # Read existing XML for metadata we want to preserve
            tree = ET.parse(xml_path)
            old_root = tree.getroot()

            # Build new XML
            xml_str = _build_arrangement_xml(
                old_root, notes, chords, chord_templates, beats, sections, metadata
            )

            # Write XML
            Path(xml_path).write_text(xml_str)

            # Try to compile XML -> SNG
            _compile_sng(xml_path)

            # Pack back to PSARC
            dlc_dir = get_dlc_dir()
            filename = session["filename"]
            output_path = dlc_dir / filename

            # Backup original
            backup = dlc_dir / (filename + ".bak")
            if output_path.exists() and not backup.exists():
                shutil.copy2(output_path, backup)

            pack_psarc(session["dir"], str(output_path))
            return str(output_path)

        try:
            output = await asyncio.get_event_loop().run_in_executor(None, _save)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        return {"success": True, "path": output}

    # ── Upload album art ───────────────────────────────────────────────

    @app.post("/api/plugins/editor/upload-art")
    async def upload_art(file: UploadFile = File(...)):
        art_id = Path(file.filename).stem.replace(" ", "_")
        ext = Path(file.filename).suffix or ".png"
        dest = STATIC_DIR / f"editor_art_{art_id}{ext}"
        content = await file.read()
        dest.write_bytes(content)
        return {"art_path": str(dest)}

    # ── Upload audio file ──────────────────────────────────────────────

    @app.post("/api/plugins/editor/upload-audio")
    async def upload_audio(file: UploadFile = File(...)):
        audio_id = Path(file.filename).stem.replace(" ", "_")
        ext = Path(file.filename).suffix or ".mp3"
        dest = STATIC_DIR / f"editor_audio_{audio_id}{ext}"
        content = await file.read()
        dest.write_bytes(content)
        return {"audio_url": f"/static/editor_audio_{audio_id}{ext}"}

    # ── Download audio from YouTube ──────────────────────────────────

    @app.post("/api/plugins/editor/youtube-audio")
    async def youtube_audio(data: dict):
        url = data.get("url", "").strip()
        if not url:
            return JSONResponse({"error": "No URL provided"}, 400)

        def _download():
            tmp = tempfile.mkdtemp(prefix="slopsmith_yt_")
            out_template = os.path.join(tmp, "audio.%(ext)s")
            try:
                import yt_dlp
                opts = {
                    "format": "bestaudio/best",
                    "outtmpl": out_template,
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "audio")

                # Find the output file
                for f in Path(tmp).iterdir():
                    if f.suffix in (".mp3", ".m4a", ".ogg", ".wav"):
                        audio_id = re.sub(r"[^a-zA-Z0-9_-]", "_", title)[:60]
                        ext = f.suffix
                        dest = STATIC_DIR / f"editor_audio_{audio_id}{ext}"
                        shutil.copy2(f, dest)
                        shutil.rmtree(tmp, ignore_errors=True)
                        return {
                            "audio_url": f"/static/editor_audio_{audio_id}{ext}",
                            "title": title,
                        }

                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError("No audio file produced")
            except Exception as e:
                shutil.rmtree(tmp, ignore_errors=True)
                raise

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _download
            )
            return result
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    # ── Import Guitar Pro file ───────────────────────────────────────

    @app.post("/api/plugins/editor/import-gp")
    async def import_gp(file: UploadFile = File(...)):
        """Upload a GP file and return track listing."""
        from lib.gp2rs import list_tracks

        tmp = tempfile.mkdtemp(prefix="slopsmith_gp_")
        gp_path = os.path.join(tmp, file.filename)
        content = await file.read()
        Path(gp_path).write_bytes(content)

        def _list():
            return list_tracks(gp_path)

        try:
            tracks = await asyncio.get_event_loop().run_in_executor(
                None, _list
            )
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return JSONResponse({"error": f"Failed to parse GP file: {e}"}, 500)

        return {"gp_path": gp_path, "tracks": tracks}

    # ── Convert GP tracks to arrangement and open in editor ──────────

    @app.post("/api/plugins/editor/convert-gp")
    async def convert_gp(data: dict):
        """Convert selected GP tracks to Rocksmith arrangements."""
        from lib.gp2rs import convert_file, auto_select_tracks
        from lib.song import parse_arrangement, Song, Beat, Section

        gp_path = data.get("gp_path", "")
        audio_url = data.get("audio_url", "")
        audio_path = data.get("audio_path", "")  # local path in container
        track_indices = data.get("track_indices")  # None = auto-select
        arrangement_names = data.get("arrangement_names")  # {idx: name}
        title = data.get("title", "")
        artist = data.get("artist", "")
        album = data.get("album", "")
        year = data.get("year", "")

        if not gp_path or not Path(gp_path).exists():
            return JSONResponse({"error": "GP file not found"}, 400)

        def _convert():
            tmp = tempfile.mkdtemp(prefix="slopsmith_editor_create_")

            # Auto-select tracks if none specified
            names_map = None
            if track_indices is None:
                indices, names_map = auto_select_tracks(gp_path)
            else:
                indices = track_indices
                if arrangement_names:
                    names_map = {int(k): v for k, v in arrangement_names.items()}

            # Convert GP to XMLs
            xml_paths = convert_file(
                gp_path, tmp,
                track_indices=indices,
                arrangement_names=names_map,
            )

            # Parse the generated XMLs into a Song object
            song = Song()
            song.title = title
            song.artist = artist
            song.album = album
            if year:
                try:
                    song.year = int(year)
                except ValueError:
                    pass

            for xml_path in xml_paths:
                arr = parse_arrangement(xml_path)
                song.arrangements.append(arr)

            # Get beats and sections from first XML
            if xml_paths:
                import xml.etree.ElementTree as XET
                tree = XET.parse(xml_paths[0])
                root = tree.getroot()

                el = root.find("songLength")
                if el is not None and el.text:
                    song.song_length = float(el.text)

                container = root.find("ebeats")
                if container is not None:
                    for eb in container.findall("ebeat"):
                        t = float(eb.get("time", "0"))
                        m = int(eb.get("measure", "-1"))
                        song.beats.append(Beat(time=t, measure=m))

                container = root.find("sections")
                if container is not None:
                    for s in container.findall("section"):
                        song.sections.append(Section(
                            name=s.get("name", ""),
                            number=int(s.get("number", "1")),
                            start_time=float(s.get("startTime", "0")),
                        ))

            # If we have a local audio file path, copy to static
            nonlocal audio_url
            if audio_path and Path(audio_path).exists():
                audio_id = re.sub(r"[^a-zA-Z0-9_-]", "_", title or "gp_import")[:60]
                ext = Path(audio_path).suffix
                dest = STATIC_DIR / f"editor_audio_{audio_id}{ext}"
                shutil.copy2(audio_path, dest)
                audio_url = f"/static/editor_audio_{audio_id}{ext}"

            result = _song_to_dict(song, audio_url)
            return result, tmp, xml_paths

        try:
            result, session_dir, xml_files = (
                await asyncio.get_event_loop().run_in_executor(None, _convert)
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        session_id = f"create_{re.sub(r'[^a-z0-9]', '', (title or 'new').lower())[:30]}"
        if session_id in sessions:
            old = sessions[session_id]
            shutil.rmtree(old["dir"], ignore_errors=True)

        sessions[session_id] = {
            "dir": session_dir,
            "audio_file": None,
            "filename": "",
            "xml_files": xml_files,
            "create_mode": True,
            "gp_path": gp_path,
            "metadata": {
                "title": title, "artist": artist,
                "album": album, "year": year,
            },
        }
        result["session_id"] = session_id
        result["create_mode"] = True
        return result

    # ── Import piano/keyboard tracks from a GP file ────────────────────

    @app.post("/api/plugins/editor/import-keys")
    async def import_keys_track(data: dict):
        """Import a piano/keyboard track from a GP file and return as an arrangement."""
        from lib.gp2rs import (
            list_tracks, convert_piano_track, is_piano_track,
            _build_tempo_map, _tick_to_seconds, GP_TICKS_PER_QUARTER,
        )
        from lib.song import parse_arrangement, Song, Beat, Section
        import guitarpro

        gp_path = data.get("gp_path", "")
        track_index = data.get("track_index")
        audio_offset = data.get("audio_offset", 0.0)

        if not gp_path or not Path(gp_path).exists():
            return JSONResponse({"error": "GP file not found"}, 400)
        if track_index is None:
            return JSONResponse({"error": "track_index required"}, 400)

        def _convert():
            song = guitarpro.parse(gp_path)
            track = song.tracks[track_index]

            if not is_piano_track(track):
                # Still allow manual override — user picked this track
                pass

            xml_str = convert_piano_track(
                song, track_index, audio_offset, "Keys"
            )

            # Write to temp file so we can parse it back
            tmp = tempfile.mkdtemp(prefix="slopsmith_keys_")
            xml_path = os.path.join(tmp, "Keys.xml")
            Path(xml_path).write_text(xml_str)

            arr = parse_arrangement(xml_path)
            arr_data = {
                "name": "Keys",
                "tuning": arr.tuning,
                "capo": arr.capo,
                "notes": [],
                "chords": [],
                "chord_templates": [],
            }

            for n in arr.notes:
                arr_data["notes"].append({
                    "time": round(n.time, 3),
                    "string": n.string,
                    "fret": n.fret,
                    "sustain": round(n.sustain, 3),
                    "techniques": {
                        "bend": n.bend,
                        "slide_to": n.slide_to,
                        "slide_unpitch_to": n.slide_unpitch_to,
                        "hammer_on": n.hammer_on,
                        "pull_off": n.pull_off,
                        "harmonic": n.harmonic,
                        "harmonic_pinch": n.harmonic_pinch,
                        "palm_mute": n.palm_mute,
                        "mute": n.mute,
                        "tremolo": n.tremolo,
                        "accent": n.accent,
                        "tap": n.tap,
                        "link_next": n.link_next,
                    },
                })

            for ch in arr.chords:
                chord_data = {
                    "time": round(ch.time, 3),
                    "chord_id": ch.chord_id,
                    "high_density": ch.high_density,
                    "notes": [],
                }
                for cn in ch.notes:
                    chord_data["notes"].append({
                        "time": round(cn.time, 3),
                        "string": cn.string,
                        "fret": cn.fret,
                        "sustain": round(cn.sustain, 3),
                        "techniques": {
                            "bend": cn.bend,
                            "slide_to": cn.slide_to,
                            "slide_unpitch_to": cn.slide_unpitch_to,
                            "hammer_on": cn.hammer_on,
                            "pull_off": cn.pull_off,
                            "harmonic": cn.harmonic,
                            "palm_mute": cn.palm_mute,
                            "mute": cn.mute,
                            "tremolo": cn.tremolo,
                            "accent": cn.accent,
                            "tap": cn.tap,
                            "link_next": cn.link_next,
                        },
                    })
                arr_data["chords"].append(chord_data)

            for ct in arr.chord_templates:
                arr_data["chord_templates"].append({
                    "name": ct.name,
                    "frets": ct.frets,
                    "fingers": ct.fingers,
                })

            return arr_data, tmp, xml_path

        try:
            arr_data, tmp_dir, xml_path = (
                await asyncio.get_event_loop().run_in_executor(None, _convert)
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        return {"arrangement": arr_data, "tmp_dir": tmp_dir, "xml_path": xml_path}

    # ── Add arrangement to existing session ──────────────────────────

    @app.post("/api/plugins/editor/add-arrangement")
    async def add_arrangement(data: dict):
        """Add a new arrangement (e.g. Keys) to the current editing session."""
        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "No active session"}, 400)

        arrangement = data.get("arrangement")
        xml_path = data.get("xml_path", "")

        if not arrangement:
            return JSONResponse({"error": "arrangement data required"}, 400)

        # If we have an XML path from import-keys, add it to session's xml_files
        if xml_path and Path(xml_path).exists():
            # Copy XML into session dir
            dest = os.path.join(session["dir"], f"Keys_{len(session.get('xml_files', []))}.xml")
            shutil.copy2(xml_path, dest)
            if "xml_files" not in session:
                session["xml_files"] = []
            session["xml_files"].append(dest)

        return {"success": True, "arrangement_count": len(session.get("xml_files", []))}

    # ── Build CDLC from create-mode session ──────────────────────────

    @app.post("/api/plugins/editor/build")
    async def build_cdlc_endpoint(data: dict):
        """Build a complete CDLC .psarc from the current create-mode session."""
        from lib.cdlc_builder import build_cdlc

        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session or not session.get("create_mode"):
            return JSONResponse({"error": "No active create session"}, 400)

        arrangements_data = data.get("arrangements", [])
        beats = data.get("beats", [])
        sections = data.get("sections", [])
        meta = data.get("metadata", session.get("metadata", {}))
        audio_url = data.get("audio_url", "")
        art_path = data.get("art_path", "")

        def _build():
            # Write each arrangement's data to its corresponding XML
            xml_files = session["xml_files"]
            for i, xml_path in enumerate(xml_files):
                tree = ET.parse(xml_path)
                old_root = tree.getroot()

                if i < len(arrangements_data):
                    arr = arrangements_data[i]
                    arr_notes = arr.get("notes", [])
                    arr_chords = arr.get("chords", [])
                    arr_templates = arr.get("chord_templates", [])
                else:
                    arr_notes, arr_chords, arr_templates = [], [], []

                xml_str = _build_arrangement_xml(
                    old_root, arr_notes, arr_chords, arr_templates,
                    beats, sections, meta,
                )
                Path(xml_path).write_text(xml_str)

            # Resolve audio file path from URL
            audio_file = ""
            if audio_url and audio_url.startswith("/static/"):
                audio_file = str(STATIC_DIR / audio_url.replace("/static/", ""))

            if not audio_file or not Path(audio_file).exists():
                raise RuntimeError("No audio file available for build")

            # Get arrangement names from XMLs, deduplicate
            arr_names = []
            name_counts = {}
            for xp in xml_files:
                root = ET.parse(xp).getroot()
                el = root.find("arrangement")
                name = el.text if el is not None and el.text else "Lead"
                name_counts[name] = name_counts.get(name, 0) + 1
                if name_counts[name] > 1:
                    name = f"{name}{name_counts[name]}"
                arr_names.append(name)
            # Also rename in the XMLs so manifests match
            for xp, name in zip(xml_files, arr_names):
                tree = ET.parse(xp)
                el = tree.getroot().find("arrangement")
                if el is not None:
                    el.text = name
                    tree.write(xp, xml_declaration=True, encoding="unicode")

            dlc_dir = get_dlc_dir()
            title = meta.get("title", "Untitled")
            artist = meta.get("artistName") or meta.get("artist", "Unknown")
            safe_t = re.sub(r'[<>:"/\\|?*]', '_', title)
            safe_a = re.sub(r'[<>:"/\\|?*]', '_', artist)
            output = str(dlc_dir / f"{safe_t}_{safe_a}_p.psarc")

            return build_cdlc(
                xml_paths=xml_files,
                arrangement_names=arr_names,
                audio_path=audio_file,
                title=title,
                artist=artist,
                album=meta.get("albumName") or meta.get("album", ""),
                year=str(meta.get("albumYear") or meta.get("year", "")),
                output_path=output,
                album_art_path=art_path if art_path and Path(art_path).exists() else "",
            )

        try:
            output_path = await asyncio.get_event_loop().run_in_executor(
                None, _build
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        return {"success": True, "path": output_path}

    # ── Helpers ──────────────────────────────────────────────────────────

    def _song_to_dict(song, audio_url):
        """Convert a Song object to JSON-serializable dict."""
        result = {
            "title": song.title,
            "artist": song.artist,
            "album": song.album,
            "year": song.year,
            "duration": song.song_length,
            "offset": song.offset,
            "audio_url": audio_url,
            "beats": [
                {"time": b.time, "measure": b.measure} for b in song.beats
            ],
            "sections": [
                {
                    "name": s.name,
                    "number": s.number,
                    "start_time": s.start_time,
                }
                for s in song.sections
            ],
            "arrangements": [],
        }

        for arr in song.arrangements:
            arr_data = {
                "name": arr.name,
                "tuning": arr.tuning,
                "capo": arr.capo,
                "notes": [],
                "chords": [],
                "chord_templates": [],
            }

            for n in arr.notes:
                arr_data["notes"].append({
                    "time": round(n.time, 3),
                    "string": n.string,
                    "fret": n.fret,
                    "sustain": round(n.sustain, 3),
                    "techniques": {
                        "bend": n.bend,
                        "slide_to": n.slide_to,
                        "slide_unpitch_to": n.slide_unpitch_to,
                        "hammer_on": n.hammer_on,
                        "pull_off": n.pull_off,
                        "harmonic": n.harmonic,
                        "harmonic_pinch": n.harmonic_pinch,
                        "palm_mute": n.palm_mute,
                        "mute": n.mute,
                        "tremolo": n.tremolo,
                        "accent": n.accent,
                        "tap": n.tap,
                        "link_next": n.link_next,
                    },
                })

            for ch in arr.chords:
                chord_data = {
                    "time": round(ch.time, 3),
                    "chord_id": ch.chord_id,
                    "high_density": ch.high_density,
                    "notes": [],
                }
                for cn in ch.notes:
                    chord_data["notes"].append({
                        "time": round(cn.time, 3),
                        "string": cn.string,
                        "fret": cn.fret,
                        "sustain": round(cn.sustain, 3),
                        "techniques": {
                            "bend": cn.bend,
                            "slide_to": cn.slide_to,
                            "slide_unpitch_to": cn.slide_unpitch_to,
                            "hammer_on": cn.hammer_on,
                            "pull_off": cn.pull_off,
                            "harmonic": cn.harmonic,
                            "palm_mute": cn.palm_mute,
                            "mute": cn.mute,
                            "tremolo": cn.tremolo,
                            "accent": cn.accent,
                            "tap": cn.tap,
                            "link_next": cn.link_next,
                        },
                    })
                arr_data["chords"].append(chord_data)

            for ct in arr.chord_templates:
                arr_data["chord_templates"].append({
                    "name": ct.name,
                    "frets": ct.frets,
                    "fingers": ct.fingers,
                })

            result["arrangements"].append(arr_data)

        return result

    def _build_arrangement_xml(
        old_root, notes, chords, chord_templates, beats, sections, metadata
    ):
        """Build a Rocksmith arrangement XML from editor data."""
        root = ET.Element("song", version="7")

        # Preserve metadata from original XML, override with editor metadata
        def _text(tag, fallback=""):
            el = old_root.find(tag)
            return metadata.get(tag, el.text if el is not None and el.text else fallback)

        ET.SubElement(root, "title").text = _text("title", "Untitled")
        ET.SubElement(root, "arrangement").text = _text("arrangement", "Lead")
        ET.SubElement(root, "offset").text = _text("offset", "0.000")
        ET.SubElement(root, "songLength").text = _text("songLength", "0.000")
        ET.SubElement(root, "startBeat").text = _text("startBeat", "0.000")
        ET.SubElement(root, "averageTempo").text = _text("averageTempo", "120")
        ET.SubElement(root, "artistName").text = _text("artistName", "Unknown")
        ET.SubElement(root, "albumName").text = _text("albumName", "")
        ET.SubElement(root, "albumYear").text = _text("albumYear", "")

        # Tuning — preserve from original
        old_tuning = old_root.find("tuning")
        tuning_el = ET.SubElement(root, "tuning")
        for i in range(6):
            val = "0"
            if old_tuning is not None:
                val = old_tuning.get(f"string{i}", "0")
            tuning_el.set(f"string{i}", val)

        old_capo = old_root.find("capo")
        ET.SubElement(root, "capo").text = (
            old_capo.text if old_capo is not None and old_capo.text else "0"
        )

        # Ebeats
        ebeats_el = ET.SubElement(root, "ebeats", count=str(len(beats)))
        for b in beats:
            ET.SubElement(
                ebeats_el, "ebeat",
                time=f"{b['time']:.3f}", measure=str(b["measure"]),
            )

        # Sections
        if not sections:
            sections = [{"name": "default", "number": 1, "start_time": 0.0}]
        sections_el = ET.SubElement(root, "sections", count=str(len(sections)))
        for s in sections:
            ET.SubElement(
                sections_el, "section",
                name=s["name"], number=str(s["number"]),
                startTime=f"{s['start_time']:.3f}",
            )

        # Phrases — one per section
        phrases_el = ET.SubElement(root, "phrases", count=str(len(sections)))
        for s in sections:
            ET.SubElement(
                phrases_el, "phrase",
                disparity="0", ignore="0", maxDifficulty="0",
                name=s["name"], solo="0",
            )

        phrase_iters = ET.SubElement(
            root, "phraseIterations", count=str(len(sections))
        )
        for i, s in enumerate(sections):
            ET.SubElement(
                phrase_iters, "phraseIteration",
                time=f"{s['start_time']:.3f}", phraseId=str(i),
            )

        # Chord templates
        ct_el = ET.SubElement(
            root, "chordTemplates", count=str(len(chord_templates))
        )
        for ct in chord_templates:
            attrs = {"chordName": ct.get("name", "")}
            frets = ct.get("frets", [-1] * 6)
            fingers = ct.get("fingers", [-1] * 6)
            for i in range(6):
                attrs[f"fret{i}"] = str(frets[i] if i < len(frets) else -1)
                attrs[f"finger{i}"] = str(fingers[i] if i < len(fingers) else -1)
            ET.SubElement(ct_el, "chordTemplate", **attrs)

        # Single difficulty level
        levels_el = ET.SubElement(root, "levels", count="1")
        level = ET.SubElement(levels_el, "level", difficulty="0")

        # Notes
        notes_el = ET.SubElement(level, "notes", count=str(len(notes)))
        for n in notes:
            techs = n.get("techniques", {})
            attrs = {
                "time": f"{n['time']:.3f}",
                "string": str(n["string"]),
                "fret": str(n["fret"]),
                "sustain": f"{n.get('sustain', 0.0):.3f}",
                "bend": f"{techs.get('bend', 0.0):.1f}",
                "hammerOn": "1" if techs.get("hammer_on") else "0",
                "pullOff": "1" if techs.get("pull_off") else "0",
                "slideTo": str(techs.get("slide_to", -1)),
                "slideUnpitchTo": str(techs.get("slide_unpitch_to", -1)),
                "harmonic": "1" if techs.get("harmonic") else "0",
                "harmonicPinch": "1" if techs.get("harmonic_pinch") else "0",
                "palmMute": "1" if techs.get("palm_mute") else "0",
                "mute": "1" if techs.get("mute") else "0",
                "tremolo": "1" if techs.get("tremolo") else "0",
                "accent": "1" if techs.get("accent") else "0",
                "linkNext": "1" if techs.get("link_next") else "0",
                "tap": "1" if techs.get("tap") else "0",
                "ignore": "0",
            }
            ET.SubElement(notes_el, "note", **attrs)

        # Chords
        chords_el = ET.SubElement(level, "chords", count=str(len(chords)))
        for ch in chords:
            chord_el = ET.SubElement(
                chords_el, "chord",
                time=f"{ch['time']:.3f}",
                chordId=str(ch.get("chord_id", 0)),
                highDensity="1" if ch.get("high_density") else "0",
                strum="down",
            )
            for cn in ch.get("notes", []):
                techs = cn.get("techniques", {})
                ET.SubElement(
                    chord_el, "chordNote",
                    time=f"{cn['time']:.3f}",
                    string=str(cn["string"]),
                    fret=str(cn["fret"]),
                    sustain=f"{cn.get('sustain', 0.0):.3f}",
                    bend=f"{techs.get('bend', 0.0):.1f}",
                    hammerOn="1" if techs.get("hammer_on") else "0",
                    pullOff="1" if techs.get("pull_off") else "0",
                    slideTo=str(techs.get("slide_to", -1)),
                    slideUnpitchTo=str(techs.get("slide_unpitch_to", -1)),
                    harmonic="1" if techs.get("harmonic") else "0",
                    harmonicPinch="1" if techs.get("harmonic_pinch") else "0",
                    palmMute="1" if techs.get("palm_mute") else "0",
                    mute="1" if techs.get("mute") else "0",
                    tremolo="1" if techs.get("tremolo") else "0",
                    accent="1" if techs.get("accent") else "0",
                    linkNext="1" if techs.get("link_next") else "0",
                    tap="1" if techs.get("tap") else "0",
                    ignore="0",
                )

        # Auto-generate anchors from note positions
        anchors = _compute_anchors(notes, chords)
        anchors_el = ET.SubElement(level, "anchors", count=str(len(anchors)))
        for a in anchors:
            ET.SubElement(
                anchors_el, "anchor",
                time=f"{a['time']:.3f}",
                fret=str(a["fret"]),
                width=str(a.get("width", 4)),
            )

        ET.SubElement(level, "handShapes", count="0")

        # Pretty print
        xml_str = ET.tostring(root, encoding="unicode")
        dom = minidom.parseString(xml_str)
        return dom.toprettyxml(indent="  ", encoding=None)

    def _compute_anchors(notes, chords):
        """Auto-generate anchors from note fret positions."""
        all_fretted = []
        for n in notes:
            if n["fret"] > 0:
                all_fretted.append((n["time"], n["fret"]))
        for ch in chords:
            for cn in ch.get("notes", []):
                if cn["fret"] > 0:
                    all_fretted.append((cn["time"], cn["fret"]))

        all_fretted.sort(key=lambda x: x[0])

        if not all_fretted:
            return [{"time": 0.0, "fret": 1, "width": 4}]

        anchors = [{
            "time": 0.0,
            "fret": max(1, all_fretted[0][1] - 1),
            "width": 4,
        }]

        for t, fret in all_fretted:
            a = anchors[-1]
            if fret < a["fret"] or fret > a["fret"] + a["width"]:
                new_fret = max(1, fret - 1)
                if new_fret != a["fret"]:
                    anchors.append({"time": t, "fret": new_fret, "width": 4})

        return anchors

    def _compile_sng(xml_path):
        """Try to compile XML to SNG via RsCli."""
        xml_p = Path(xml_path)
        sng_dir = xml_p.parent.parent / "bin" / "generic"
        sng_path = sng_dir / (xml_p.stem + ".sng")

        if not sng_path.exists():
            # No existing SNG to replace — CDLC may use XML directly
            return

        rscli = os.environ.get("RSCLI_PATH", "")
        if not rscli or not Path(rscli).exists():
            for p in ["/opt/rscli/RsCli", "./rscli/RsCli"]:
                if Path(p).exists():
                    rscli = p
                    break

        if not rscli:
            print("[Editor] RsCli not found, skipping SNG compilation")
            return

        try:
            result = subprocess.run(
                [rscli, "xml2sng", str(xml_path), str(sng_path), "pc"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                print(f"[Editor] xml2sng failed: {result.stderr}")
        except Exception as e:
            print(f"[Editor] xml2sng error: {e}")
