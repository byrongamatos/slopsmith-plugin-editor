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

    from lib.song import load_song, arrangement_to_wire, arrangement_from_wire
    from lib.psarc import unpack_psarc
    from lib.patcher import pack_psarc
    from lib.audio import find_wem_files, convert_wem
    from lib import sloppak as sloppak_mod
    import json
    import yaml
    import zipfile

    STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
    SLOPPAK_CACHE = STATIC_DIR / "sloppak_cache"

    # Active editing sessions: session_id -> {dir, audio_file, filename, song_data}
    sessions = {}

    def _arrangement_id(name: str, used: set) -> str:
        """Map an arrangement name to a stable filesystem-safe id, avoiding collisions."""
        base = re.sub(r"[^a-z0-9_]", "", (name or "arr").lower().replace(" ", "_")) or "arr"
        aid = base
        i = 2
        while aid in used:
            aid = f"{base}{i}"
            i += 1
        used.add(aid)
        return aid

    # ── List available CDLC files ────────────────────────────────────────

    @app.get("/api/plugins/editor/songs")
    async def list_songs():
        dlc_dir = get_dlc_dir()
        if not dlc_dir or not dlc_dir.exists():
            return []
        files = []
        for f in dlc_dir.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix == ".psarc":
                files.append({"filename": str(f.relative_to(dlc_dir)), "format": "psarc"})
            elif f.suffix == ".sloppak":
                files.append({"filename": str(f.relative_to(dlc_dir)), "format": "sloppak"})
        files.sort(key=lambda x: x["filename"])
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

        is_sloppak = filepath.suffix == ".sloppak"

        def _load_psarc():
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
            result["format"] = "psarc"
            return result, tmp_dir, audio_file, xml_files, None

        def _load_sloppak():
            SLOPPAK_CACHE.mkdir(parents=True, exist_ok=True)
            loaded = sloppak_mod.load_song(filename, dlc_dir, SLOPPAK_CACHE)
            song = loaded.song

            # Build a per-arrangement id list from the manifest so we can map
            # edits back to the correct JSON file on save.
            arrangement_ids = []
            for entry in (loaded.manifest.get("arrangements", []) or []):
                arrangement_ids.append(entry.get("id", ""))

            # Pick an audio URL: prefer the "full" stem, else the first stem.
            audio_url = None
            audio_file = None
            stem_path = None
            for s in loaded.stems:
                if s.get("id") == "full":
                    stem_path = loaded.source_dir / s.get("file", "")
                    break
            if stem_path is None and loaded.stems:
                stem_path = loaded.source_dir / loaded.stems[0].get("file", "")
            if stem_path and stem_path.exists():
                audio_id = Path(filename).stem.replace(" ", "_").replace("/", "_")
                ext = stem_path.suffix
                dest = STATIC_DIR / f"editor_audio_{audio_id}{ext}"
                shutil.copy2(stem_path, dest)
                audio_url = f"/static/editor_audio_{audio_id}{ext}"
                audio_file = str(stem_path)

            result = _song_to_dict(song, audio_url)
            result["format"] = "sloppak"
            # Carry the manifest-derived arrangement id list onto each
            # arrangement so the frontend can round-trip it back to us.
            for i, arr_data in enumerate(result.get("arrangements", [])):
                arr_data["id"] = arrangement_ids[i] if i < len(arrangement_ids) else _arrangement_id(arr_data["name"], set())

            return (
                result,
                str(loaded.source_dir),  # working dir = the unpacked sloppak cache
                audio_file,
                None,                    # no xml_files for sloppak
                {
                    "manifest": loaded.manifest,
                    "arrangement_ids": arrangement_ids,
                },
            )

        try:
            if is_sloppak:
                result, session_dir, audio_file, xml_files, sloppak_state = (
                    await asyncio.get_event_loop().run_in_executor(None, _load_sloppak)
                )
            else:
                result, session_dir, audio_file, xml_files, sloppak_state = (
                    await asyncio.get_event_loop().run_in_executor(None, _load_psarc)
                )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        session_id = Path(filename).stem
        # Clean up previous PSARC session for same file (sloppak sessions
        # use the cache dir directly — never delete it on session swap).
        if session_id in sessions:
            old = sessions[session_id]
            if old.get("format") == "psarc":
                shutil.rmtree(old["dir"], ignore_errors=True)

        sessions[session_id] = {
            "dir": session_dir,
            "audio_file": audio_file,
            "filename": filename,
            "xml_files": xml_files,
            "format": "sloppak" if is_sloppak else "psarc",
            "sloppak_state": sloppak_state,
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

        # Sloppak save can be a full snapshot of all arrangements (needed when
        # arrangements were added). If arrangements isn't provided, save_cdlc
        # only updates the single arrangement at arrangement_index.
        all_arrangements = data.get("arrangements")

        def _save_psarc():
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

        def _save_sloppak():
            sloppak_state = session.get("sloppak_state") or {}
            manifest = dict(sloppak_state.get("manifest") or {})
            existing_ids = list(sloppak_state.get("arrangement_ids") or [])
            source_dir = Path(session["dir"])
            dlc_dir = get_dlc_dir()
            filename = session["filename"]
            output_path = dlc_dir / filename

            # Determine the arrangement set to write. If `arrangements` was
            # provided, it's the authoritative full snapshot (handles adds
            # and reorders). Otherwise we update only the single arrangement
            # at arrangement_index using notes/chords/chord_templates.
            if all_arrangements is None:
                if arrangement_index >= len(existing_ids):
                    raise RuntimeError("Invalid arrangement index")
                # Fetch the current manifest entry for the edited arrangement
                # so we keep its name/tuning/capo.
                old_entries = manifest.get("arrangements", []) or []
                if arrangement_index >= len(old_entries):
                    raise RuntimeError("Manifest arrangement out of range")
                old_entry = old_entries[arrangement_index]
                merged_arrangements = []
                # We need to reconstruct the wire data for the edited
                # arrangement only; other arrangements stay untouched.
                for i, entry in enumerate(old_entries):
                    if i == arrangement_index:
                        wire = _arr_dict_to_wire(
                            entry.get("name", ""),
                            entry.get("tuning", [0]*6),
                            int(entry.get("capo", 0)),
                            notes, chords, chord_templates,
                        )
                        # First arrangement carries beats/sections.
                        if i == 0:
                            wire["beats"] = [
                                {"time": round(float(b.get("time", 0)), 3),
                                 "measure": int(b.get("measure", -1))}
                                for b in beats
                            ]
                            wire["sections"] = [
                                {"name": s.get("name", ""),
                                 "number": int(s.get("number", 0)),
                                 "time": round(float(s.get("start_time", 0)), 3)}
                                for s in sections
                            ]
                        merged_arrangements.append({
                            "entry": entry,
                            "wire": wire,
                        })
                    else:
                        merged_arrangements.append({"entry": entry, "wire": None})
            else:
                # Full snapshot path — used when arrangements were added/removed
                # or for safety on every save.
                used_ids: set = set()
                merged_arrangements = []
                first = True
                for i, ad in enumerate(all_arrangements):
                    aid = ad.get("id") or _arrangement_id(ad.get("name", "arr"), used_ids)
                    used_ids.add(aid)
                    wire = _arr_dict_to_wire(
                        ad.get("name", "arr"),
                        ad.get("tuning", [0]*6),
                        int(ad.get("capo", 0)),
                        ad.get("notes", []),
                        ad.get("chords", []),
                        ad.get("chord_templates", []),
                    )
                    if first:
                        wire["beats"] = [
                            {"time": round(float(b.get("time", 0)), 3),
                             "measure": int(b.get("measure", -1))}
                            for b in beats
                        ]
                        wire["sections"] = [
                            {"name": s.get("name", ""),
                             "number": int(s.get("number", 0)),
                             "time": round(float(s.get("start_time", 0)), 3)}
                            for s in sections
                        ]
                        first = False
                    merged_arrangements.append({
                        "entry": {
                            "id": aid,
                            "name": ad.get("name", "arr"),
                            "file": f"arrangements/{aid}.json",
                            "tuning": list(ad.get("tuning", [0]*6)),
                            "capo": int(ad.get("capo", 0)),
                        },
                        "wire": wire,
                    })

            # Write/update arrangement JSON files inside source_dir
            arr_dir = source_dir / "arrangements"
            arr_dir.mkdir(parents=True, exist_ok=True)
            new_manifest_arrangements = []
            kept_paths: set[Path] = set()
            for item in merged_arrangements:
                entry = item["entry"]
                wire = item["wire"]
                if wire is not None:
                    # Determine target path — fall back to default if missing
                    rel = entry.get("file") or f"arrangements/{entry.get('id', 'arr')}.json"
                    arr_path = source_dir / rel
                    arr_path.parent.mkdir(parents=True, exist_ok=True)
                    arr_path.write_text(
                        json.dumps(wire, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    entry = dict(entry)
                    entry["file"] = rel
                # Track every kept arrangement file (rewritten or untouched)
                rel = entry.get("file")
                if rel:
                    kept_paths.add((source_dir / rel).resolve())
                new_manifest_arrangements.append(entry)
            manifest["arrangements"] = new_manifest_arrangements

            # Drop orphaned arrangement JSONs (e.g. after a remove). Only
            # touches files inside the arrangements/ subdir to be safe.
            if arr_dir.exists():
                for f in arr_dir.glob("*.json"):
                    if f.resolve() not in kept_paths:
                        try:
                            f.unlink()
                        except OSError:
                            pass

            # Apply edited top-level metadata (title/artist/album/year only —
            # don't let the editor overwrite stems/lyrics/cover paths).
            if metadata:
                for k in ("title", "artist", "album"):
                    if metadata.get(k) is not None:
                        manifest[k] = metadata[k]
                if metadata.get("year") is not None:
                    try:
                        manifest["year"] = int(metadata["year"])
                    except (TypeError, ValueError):
                        pass

            # Write manifest.yaml back into the source dir
            (source_dir / "manifest.yaml").write_text(
                yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

            # Backup original .sloppak (zip) if not already
            if output_path.exists():
                backup = dlc_dir / (filename + ".bak")
                if not backup.exists():
                    shutil.copy2(output_path, backup)

            # Re-zip the source dir into the .sloppak file
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_zip = output_path.with_suffix(output_path.suffix + ".tmp")
            with zipfile.ZipFile(str(tmp_zip), "w", zipfile.ZIP_DEFLATED) as zf:
                for f in source_dir.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(source_dir).as_posix())
            tmp_zip.replace(output_path)
            return str(output_path)

        try:
            if session.get("format") == "sloppak":
                output = await asyncio.get_event_loop().run_in_executor(None, _save_sloppak)
            else:
                output = await asyncio.get_event_loop().run_in_executor(None, _save_psarc)
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

    # ── MIDI import: list tracks ─────────────────────────────────────

    @app.post("/api/plugins/editor/import-midi")
    async def import_midi(file: UploadFile = File(...)):
        """Upload a MIDI file and return track listing."""
        from lib.midi_import import list_midi_tracks

        suffix = Path(file.filename or "song.mid").suffix or ".mid"
        tmp = tempfile.mkdtemp(prefix="slopsmith_midi_")
        midi_path = os.path.join(tmp, "upload" + suffix)
        content = await file.read()
        Path(midi_path).write_bytes(content)

        def _list():
            return list_midi_tracks(midi_path)

        try:
            tracks = await asyncio.get_event_loop().run_in_executor(None, _list)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return JSONResponse({"error": f"Failed to parse MIDI file: {e}"}, 500)

        return {"midi_path": midi_path, "tracks": tracks}

    # ── MIDI import: convert a track to a Keys arrangement ────────────

    @app.post("/api/plugins/editor/import-keys-midi")
    async def import_keys_midi(data: dict):
        """Convert a MIDI track into a Keys arrangement (editor-ready dict)."""
        from lib.midi_import import convert_midi_track_to_keys_wire

        midi_path = data.get("midi_path", "")
        track_index = data.get("track_index")
        audio_offset = float(data.get("audio_offset", 0.0))

        if not midi_path or not Path(midi_path).exists():
            return JSONResponse({"error": "MIDI file not found"}, 400)
        if track_index is None:
            return JSONResponse({"error": "track_index required"}, 400)

        def _convert():
            wire = convert_midi_track_to_keys_wire(
                midi_path, int(track_index), audio_offset, "Keys"
            )
            # Convert wire → editor's long-named shape so the frontend can
            # consume it identically to import-keys output.
            arr_data = {
                "name": wire["name"],
                "tuning": wire["tuning"],
                "capo": wire["capo"],
                "notes": [],
                "chords": [],
                "chord_templates": [],
            }
            for n in wire["notes"]:
                arr_data["notes"].append({
                    "time": n["t"],
                    "string": n["s"],
                    "fret": n["f"],
                    "sustain": n["sus"],
                    "techniques": {
                        "bend": n.get("bn", 0),
                        "slide_to": n.get("sl", -1),
                        "slide_unpitch_to": n.get("slu", -1),
                        "hammer_on": n.get("ho", False),
                        "pull_off": n.get("po", False),
                        "harmonic": n.get("hm", False),
                        "harmonic_pinch": n.get("hp", False),
                        "palm_mute": n.get("pm", False),
                        "mute": n.get("mt", False),
                        "tremolo": n.get("tr", False),
                        "accent": n.get("ac", False),
                        "tap": n.get("tp", False),
                        "link_next": False,
                    },
                })
            return arr_data

        try:
            arr_data = await asyncio.get_event_loop().run_in_executor(None, _convert)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        return {"arrangement": arr_data}

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

    # ── Import drum/percussion tracks from a GP file ─────────────────

    @app.post("/api/plugins/editor/import-drums")
    async def import_drums_track(data: dict):
        """Import a drum/percussion track from a GP file and return as an arrangement."""
        from lib.gp2rs import (
            list_tracks, convert_drum_track, is_drum_track,
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

            xml_str = convert_drum_track(
                song, track_index, audio_offset, "Drums"
            )

            # Write to temp file so we can parse it back
            tmp = tempfile.mkdtemp(prefix="slopsmith_drums_")
            xml_path = os.path.join(tmp, "Drums.xml")
            Path(xml_path).write_text(xml_str)

            arr = parse_arrangement(xml_path)
            arr_data = {
                "name": "Drums",
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

    # ── Remove arrangement from session ────────────────────────────

    @app.post("/api/plugins/editor/remove-arrangement")
    async def remove_arrangement(data: dict):
        """Remove an arrangement from the current editing session."""
        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "No active session"}, 400)

        idx = data.get("arrangement_index", -1)

        # Sloppak: nothing to remove server-side until save. The frontend
        # splices its in-memory arrangements and the next save rewrites
        # the manifest + drops the orphaned arrangement JSON.
        if session.get("format") == "sloppak":
            return {"success": True, "arrangement_count": -1, "format": "sloppak"}

        xml_files = session.get("xml_files") or []
        if 0 <= idx < len(xml_files):
            removed = xml_files.pop(idx)
            # Delete the XML file
            try:
                Path(removed).unlink(missing_ok=True)
            except Exception:
                pass

        return {"success": True, "arrangement_count": len(xml_files)}

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

        # Sloppak sessions don't use XML on disk — the save endpoint writes
        # arrangement JSON files when the user commits. The frontend keeps
        # the new arrangement in S.arrangements and sends the full snapshot
        # at save time.
        if session.get("format") == "sloppak":
            return {"success": True, "arrangement_count": -1, "format": "sloppak"}

        # PSARC path: persist the XML so save can use the existing flow.
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

    def _arr_dict_to_wire(name, tuning, capo, notes, chords, chord_templates):
        """Convert editor's long-named arrangement dict into sloppak wire format.

        Editor uses {time, string, fret, sustain, techniques: {bend, slide_to,
        ...}}; the wire format uses {t, s, f, sus, sl, bn, ho, ...}.
        """
        def _note(n):
            tech = n.get("techniques", {}) or {}
            out = {
                "t": round(float(n.get("time", 0)), 3),
                "s": int(n.get("string", 0)),
                "f": int(n.get("fret", 0)),
                "sus": round(float(n.get("sustain", 0)), 3),
                "sl": int(tech.get("slide_to", -1)),
                "slu": int(tech.get("slide_unpitch_to", -1)),
                "bn": round(float(tech.get("bend", 0) or 0), 1),
                "ho": bool(tech.get("hammer_on", False)),
                "po": bool(tech.get("pull_off", False)),
                "hm": bool(tech.get("harmonic", False)),
                "hp": bool(tech.get("harmonic_pinch", False)),
                "pm": bool(tech.get("palm_mute", False)),
                "mt": bool(tech.get("mute", False)),
                "tr": bool(tech.get("tremolo", False)),
                "ac": bool(tech.get("accent", False)),
                "tp": bool(tech.get("tap", False)),
            }
            return out

        def _note_in_chord(n):
            # Chord-member notes share the chord's time, so we omit `t`.
            d = _note(n)
            d.pop("t", None)
            return d

        wire = {
            "name": name,
            "tuning": list(tuning),
            "capo": int(capo),
            "notes": [_note(n) for n in notes],
            "chords": [
                {
                    "t": round(float(c.get("time", 0)), 3),
                    "id": int(c.get("chord_id", -1)),
                    "hd": bool(c.get("high_density", False)),
                    "notes": [_note_in_chord(cn) for cn in c.get("notes", [])],
                }
                for c in chords
            ],
            "anchors": [],
            "handshapes": [],
            "templates": [
                {
                    "name": ct.get("name", ""),
                    "fingers": list(ct.get("fingers", [-1]*6)),
                    "frets": list(ct.get("frets", [-1]*6)),
                }
                for ct in chord_templates
            ],
        }
        return wire

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
