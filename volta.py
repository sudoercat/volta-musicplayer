#!/usr/bin/env python3
"""
Volta — Lecteur de musique natif
Lance avec : python volta.py
Lance avec une médiathèque custom : python volta.py /chemin/vers/musique
"""
import sys, os, json, csv, re, threading, socket
from pathlib import Path
from functools import partial

# ── Trouver un port libre ────────────────────────────────────────────────────
def find_free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

PORT = find_free_port()

# ── Dossier de base : fonctionne en .py ET en .exe PyInstaller ──────────────
def base_dir():
    """Retourne le dossier contenant l'exe (ou le .py en dev)."""
    if getattr(sys, 'frozen', False):
        # Mode .exe PyInstaller : sys.executable = .../Volta.exe
        return Path(sys.executable).parent
    else:
        # Mode .py normal
        return Path(__file__).parent

BASE = base_dir()

# ── Config médiathèque ───────────────────────────────────────────────────────
if len(sys.argv) > 1:
    MUSIC_ROOT = Path(sys.argv[1]).resolve()
else:
    MUSIC_ROOT = (BASE / "music_library").resolve()

DATA_DIR       = BASE / "data"
DATA_DIR.mkdir(exist_ok=True)
PLAYLISTS_FILE = DATA_DIR / "playlists.json"

# ════════════════════════════════════════════════════════════════════════════
# BACKEND FLASK (tourne dans un thread daemon)
# ════════════════════════════════════════════════════════════════════════════
def hide_console():
    """Cache la fenêtre console sur Windows dès que possible."""
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass

def start_backend():
    import flask
    from flask import Flask, jsonify, send_file, request, abort
    import logging, os
    # Supprime tous les logs werkzeug / Flask
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    log.propagate = False
    logging.getLogger('flask').setLevel(logging.ERROR)
    logging.disable(logging.CRITICAL)

    app = Flask(__name__)

    # ── helpers ──────────────────────────────────────────────────────────────
    def load_playlists():
        if PLAYLISTS_FILE.exists():
            return json.loads(PLAYLISTS_FILE.read_text(encoding='utf-8'))
        return {}

    def save_playlists(data):
        PLAYLISTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    IMAGE_EXTS = ('*.webp','*.jpg','*.jpeg','*.png','*.gif','*.bmp','*.tiff','*.avif')
    PRIO_NAMES  = ('cover','folder','front','artwork','thumb','album','poster')

    def find_cover(directory):
        """Cherche une image dans directory.
        Priorité : fichiers nommés cover.*, folder.*, front.*, etc.
        Fallback  : première image trouvée."""
        directory = Path(directory)
        if not directory.is_dir():
            return None
        # 1. Cherche par nom prioritaire (cover.*, folder.*, ...)
        for name in PRIO_NAMES:
            for ext in ('.webp','.jpg','.jpeg','.png','.gif','.bmp','.avif'):
                p = directory / (name + ext)
                if p.exists():
                    return p.name
        # 2. Fallback : première image dans le dossier
        for ext in IMAGE_EXTS:
            found = sorted(directory.glob(ext))
            if found:
                return found[0].name
        return None

    def find_artist_cover(artist_dir):
        """Cherche la PP d'un artiste :
        1. Image directement dans le dossier artiste (cover.*, etc.)
        2. Sinon cover du premier album trouvé."""""
        artist_dir = Path(artist_dir)
        # 1. Image dans le dossier artiste lui-même
        cover = find_cover(artist_dir)
        if cover:
            return ('artist', cover)  # (type, filename)
        # 2. Fallback : premier album avec une cover
        try:
            for album_dir in sorted(artist_dir.iterdir()):
                if not album_dir.is_dir(): continue
                cover = find_cover(album_dir)
                if cover:
                    rel = album_dir.relative_to(MUSIC_ROOT).as_posix()
                    return ('album', rel + '/' + cover)
        except Exception:
            pass
        return None

    def parse_tracks_csv(tf):
        """Lit tracks.csv en acceptant différents noms de colonnes."""
        tracks = []
        encodings = ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']
        for enc in encodings:
            try:
                with open(tf, newline='', encoding=enc) as f:
                    content = f.read()
                if not content.strip():
                    return []
                from io import StringIO
                reader = csv.DictReader(StringIO(content))
                # Normalise les noms de colonnes (minuscules, sans espaces)
                for row in reader:
                    norm = {k.lower().strip(): v for k, v in row.items()}
                    # Cherche le nom de fichier sous différents noms de colonne
                    filename = (norm.get('filename') or norm.get('file') or
                                norm.get('fichier') or norm.get('track') or
                                norm.get('name') or '')
                    title = (norm.get('title') or norm.get('titre') or
                             norm.get('name') or norm.get('nom') or
                             filename.replace('.webm', '') if filename else '')
                    duration = (norm.get('duration') or norm.get('durée') or
                                norm.get('duree') or norm.get('length') or '')
                    if filename:
                        tracks.append({'filename': filename.strip(),
                                       'title': title.strip(),
                                       'duration': duration.strip()})
                return tracks
            except Exception:
                continue
        return []

    def scan_library():
        artists = []
        if not MUSIC_ROOT.exists():
            return artists

        try:
            artist_dirs = sorted(
                [d for d in MUSIC_ROOT.iterdir() if d.is_dir()],
                key=lambda d: d.name.lower()
            )
        except Exception:
            return artists

        for artist_dir in artist_dirs:
            # Ignore les dossiers cachés (.git, __pycache__, etc.)
            if artist_dir.name.startswith('.') or artist_dir.name.startswith('__'):
                continue

            artist_cover = find_artist_cover(artist_dir)
            artist = {
                'name': artist_dir.name,
                'albums': [],
                'cover_type': artist_cover[0] if artist_cover else None,
                'cover_path': artist_cover[1] if artist_cover else None,
            }

            try:
                album_dirs = sorted(
                    [d for d in artist_dir.iterdir() if d.is_dir()],
                    key=lambda d: d.name.lower()
                )
            except Exception:
                continue

            for album_dir in album_dirs:
                if album_dir.name.startswith('.') or album_dir.name.startswith('__'):
                    continue

                # ── Métadonnées ──────────────────────────────────────────────
                meta = {}
                for meta_name in ('album_metadata.json', 'metadata.json', 'info.json'):
                    mf = album_dir / meta_name
                    if mf.exists():
                        for enc in ('utf-8', 'utf-8-sig', 'latin-1'):
                            try:
                                meta = json.loads(mf.read_text(encoding=enc))
                                break
                            except Exception:
                                continue
                        break

                # ── Pistes ───────────────────────────────────────────────────
                tracks = []

                # 1. Essai via tracks.csv
                tf = album_dir / 'tracks.csv'
                if tf.exists():
                    tracks = parse_tracks_csv(tf)

                # 2. Fallback : scan direct des .webm
                #    Utilisé si pas de CSV, ou si CSV ne référence pas de .webm existants
                # Tous les formats supportés par le navigateur intégré
                AUDIO_EXTS = ('*.webm','*.mp3','*.flac','*.wav','*.ogg',
                               '*.opus','*.aac','*.m4a','*.mp4','*.weba')
                audio_files = []
                for ext in AUDIO_EXTS:
                    audio_files += list(album_dir.glob(ext))
                audio_files = sorted(audio_files, key=lambda f: f.name.lower())

                if not tracks:
                    tracks = [{'filename': af.name,
                                'title':    af.stem,
                                'duration': ''}
                               for af in audio_files]
                else:
                    existing = {af.name for af in audio_files}
                    valid = [t for t in tracks if t['filename'] in existing]
                    if not valid and audio_files:
                        tracks = [{'filename': af.name,
                                    'title':    af.stem,
                                    'duration': ''}
                                   for af in audio_files]
                    elif valid:
                        tracks = valid

                # ── Pochette ─────────────────────────────────────────────────
                cover_name = find_cover(album_dir)

                # ── Chemin relatif (séparateurs forward-slash pour le web) ───
                try:
                    rel = album_dir.relative_to(MUSIC_ROOT).as_posix()
                except ValueError:
                    rel = str(album_dir)

                artist['albums'].append({
                    'id':         f"{artist_dir.name}|{album_dir.name}",
                    'name':       meta.get('album', album_dir.name),
                    'artist':     meta.get('artist', artist_dir.name),
                    'year':       str(meta.get('year', '')),
                    'genre':      meta.get('genre', ''),
                    'path':       rel,
                    'has_cover':  cover_name is not None,
                    'cover_name': cover_name or '',
                    'tracks':     tracks,
                })

            # N'ajoute l'artiste que s'il a au moins un album avec des pistes
            if artist['albums']:
                artists.append(artist)

        return artists

    # ── routes API ───────────────────────────────────────────────────────────
    @app.route('/api/library')
    def api_library():
        return jsonify(scan_library())

    @app.route('/api/debug')
    def api_debug():
        info = {
            'music_root': str(MUSIC_ROOT),
            'exists': MUSIC_ROOT.exists(),
            'artists': []
        }
        if MUSIC_ROOT.exists():
            for d in sorted(MUSIC_ROOT.iterdir()):
                if not d.is_dir(): continue
                entry = {'name': d.name, 'albums': []}
                for ad in sorted(d.iterdir()):
                    if not ad.is_dir(): continue
                    audio = [f for ext in ('*.webm','*.mp3','*.flac','*.wav','*.ogg','*.opus','*.aac','*.m4a') for f in ad.glob(ext)]
                    entry['albums'].append({
                        'name': ad.name,
                        'webm_count': len(audio),
                        'webm_files': [f.name for f in audio[:5]],
                        'has_csv': (ad / 'tracks.csv').exists(),
                        'has_meta': (ad / 'album_metadata.json').exists(),
                        'cover': find_cover(ad),
                        'all_files': [f.name for f in sorted(ad.iterdir()) if f.is_file()][:20],
                    })
                info['artists'].append(entry)
        return jsonify(info)

    MIME_MAP = {'.webp':'image/webp','.jpg':'image/jpeg','.jpeg':'image/jpeg',
                '.png':'image/png','.gif':'image/gif','.bmp':'image/bmp',
                '.avif':'image/avif','.tiff':'image/tiff'}

    @app.route('/api/cover/<path:rel>')
    def api_cover(rel):
        full = MUSIC_ROOT / rel
        # rel peut pointer directement vers un fichier image
        if full.is_file():
            mime = MIME_MAP.get(full.suffix.lower(), 'image/jpeg')
            return send_file(full, mimetype=mime)
        # Sinon cherche dans le dossier
        cover = find_cover(full)
        if cover:
            mime = MIME_MAP.get(Path(cover).suffix.lower(), 'image/jpeg')
            return send_file(full / cover, mimetype=mime)
        abort(404)

    @app.route('/api/artist_cover/<path:artist_name>')
    def api_artist_cover(artist_name):
        artist_dir = MUSIC_ROOT / artist_name
        cover_info = find_artist_cover(artist_dir)
        if not cover_info:
            abort(404)
        ctype, cpath = cover_info
        p = (artist_dir / cpath) if ctype == 'artist' else (MUSIC_ROOT / cpath)
        if p.exists():
            mime = MIME_MAP.get(p.suffix.lower(), 'image/jpeg')
            return send_file(p, mimetype=mime)
        abort(404)

    @app.route('/api/playlist_cover/<pid>', methods=['POST'])
    def upload_playlist_cover(pid):
        import base64
        pls = load_playlists()
        if pid not in pls: abort(404)
        data      = request.json
        img_b64   = data.get('image_b64', '')
        img_ext   = data.get('ext', 'jpg').lstrip('.')
        covers_dir = DATA_DIR / 'playlist_covers'
        covers_dir.mkdir(exist_ok=True)
        for old_f in covers_dir.glob(f"{pid}.*"):
            old_f.unlink(missing_ok=True)
        cover_path = covers_dir / f"{pid}.{img_ext}"
        cover_path.write_bytes(base64.b64decode(img_b64))
        pls[pid]['cover_file'] = f"{pid}.{img_ext}"
        save_playlists(pls)
        return jsonify({'ok': True, 'cover_file': pls[pid]['cover_file']})

    @app.route('/api/playlist_cover/<pid>', methods=['GET'])
    def get_playlist_cover(pid):
        pls = load_playlists()
        if pid not in pls: abort(404)
        cf = pls[pid].get('cover_file')
        if cf:
            p = DATA_DIR / 'playlist_covers' / cf
            if p.exists():
                mime = MIME_MAP.get(p.suffix.lower(), 'image/jpeg')
                return send_file(p, mimetype=mime)
        abort(404)


    AUDIO_MIME = {
        '.webm': 'audio/webm',
        '.mp3':  'audio/mpeg',
        '.flac': 'audio/flac',
        '.wav':  'audio/wav',
        '.ogg':  'audio/ogg',
        '.opus': 'audio/ogg; codecs=opus',
        '.aac':  'audio/aac',
        '.m4a':  'audio/mp4',
        '.mp4':  'audio/mp4',
        '.weba': 'audio/webm',
    }

    @app.route('/api/track/<path:rel>')
    def api_track(rel):
        from urllib.parse import unquote
        # Flask decode déjà les %XX, mais double-sécurité pour les cas limites
        rel_decoded = unquote(rel)
        p = MUSIC_ROOT / rel_decoded
        if not p.exists():
            p = MUSIC_ROOT / rel  # essai sans décodage
        mime = AUDIO_MIME.get(p.suffix.lower())
        if p.exists() and mime:
            return send_file(p, mimetype=mime, conditional=True)
        # Log pour debug
        import logging
        logging.getLogger('volta').warning(f'Track introuvable: {p}')
        abort(404)

    THEME_FILE = DATA_DIR / 'theme.json'

    @app.route('/api/theme', methods=['GET'])
    def get_theme():
        if THEME_FILE.exists():
            return THEME_FILE.read_text(encoding='utf-8'), 200, {'Content-Type': 'application/json'}
        return '{"idx":0,"dark":true}', 200, {'Content-Type': 'application/json'}

    @app.route('/api/theme', methods=['POST'])
    def save_theme():
        THEME_FILE.write_text(request.data.decode('utf-8'), encoding='utf-8')
        return '{"ok":true}', 200, {'Content-Type': 'application/json'}

    @app.route('/api/lang')
    def api_lang():
        import locale
        lang = 'en'
        try:
            sys_locale = (locale.getlocale()[0] or '') if hasattr(locale,'getlocale') else ''
            code = sys_locale.lower().split('_')[0]
            supported = {'fr','en','de','es','it','pt','nl','pl','ru'}
            if code in supported:
                lang = code
        except Exception:
            pass
        return jsonify({'lang': lang})

    @app.route('/api/playlists', methods=['GET'])
    def get_playlists():
        return jsonify(load_playlists())

    @app.route('/api/playlists', methods=['POST'])
    def create_playlist():
        d = request.json
        name = (d.get('name') or '').strip()
        if not name: return jsonify({'error': 'Name required'}), 400
        pls = load_playlists()
        pid = re.sub(r'[^a-z0-9_]', '_', name.lower()) + f'_{len(pls)}'
        pls[pid] = {'name': name, 'tracks': [], 'color': d.get('color', '#7c6af7')}
        save_playlists(pls)
        return jsonify({'id': pid, **pls[pid]})

    @app.route('/api/playlists/<pid>', methods=['PUT'])
    def update_playlist(pid):
        pls = load_playlists()
        if pid not in pls: return jsonify({'error': 'Not found'}), 404
        d = request.json
        for k in ('name', 'tracks', 'color'):
            if k in d: pls[pid][k] = d[k]
        save_playlists(pls)
        return jsonify({'id': pid, **pls[pid]})

    @app.route('/api/playlists/<pid>', methods=['DELETE'])
    def delete_playlist(pid):
        pls = load_playlists()
        if pid not in pls: return jsonify({'error': 'Not found'}), 404
        del pls[pid]
        save_playlists(pls)
        return jsonify({'ok': True})

    @app.route('/')
    def index():
        return build_html()

    app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False)


def build_html():
    """Génère le HTML complet de l'interface."""
    return r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Volta</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bamum&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0a0f;--bg2:#111118;--bg3:#18181f;--bg4:#1e1e2a;
  --surface:#1c1c26;--surface2:#252535;
  --accent:#7c6af7;--accent2:#a89eff;--accent-dim:rgba(124,106,247,.18);
  --green:#1ed760;
  --text:#f0efff;--text2:#9896b8;--text3:#4e4c66;
  --border:rgba(255,255,255,.06);--border2:rgba(255,255,255,.13);
  --sb:240px;--np:300px;--bar:88px;--r:12px;--rl:18px;
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);
  font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;-webkit-font-smoothing:antialiased}
#app{display:grid;grid-template-columns:var(--sb) 1fr var(--np);
     grid-template-rows:1fr var(--bar);height:100vh}
#sidebar{grid-row:1;grid-column:1;display:flex;flex-direction:column;
  background:var(--bg);border-right:1px solid var(--border);overflow:hidden}
#main{grid-row:1;grid-column:2;overflow-y:auto;background:var(--bg2);position:relative}
#nowpanel{grid-row:1;grid-column:3;background:var(--bg);border-left:1px solid var(--border);
  display:flex;flex-direction:column;overflow:hidden}
#playerbar{grid-row:2;grid-column:1/4;background:var(--bg3);border-top:1px solid var(--border);
  display:flex;align-items:center;padding:0 24px;gap:20px;z-index:100}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:9px}

/* SIDEBAR */
.slogo{padding:22px 20px 14px;display:flex;align-items:center;gap:10px}
.slogo-mark{width:32px;height:32px;background:var(--accent);border-radius:8px;
  display:grid;place-items:center;flex-shrink:0}
.slogo-mark svg{width:18px;height:18px;fill:white}
.slogo-text{font-size:18px;font-weight:800;letter-spacing:-.5px}
.volta-glyph{font-family:'Noto Sans Bamum',sans-serif;font-size:1.1em}
.nav-item{display:flex;align-items:center;gap:12px;padding:9px 20px;color:var(--text2);
  cursor:pointer;transition:all .15s;user-select:none;white-space:nowrap;border-radius:0}
.nav-item:hover{color:var(--text);background:rgba(255,255,255,.04)}
.nav-item.active{color:var(--text);background:rgba(255,255,255,.06);font-weight:500}
.nav-item svg{width:18px;height:18px;flex-shrink:0;opacity:.7}
.nav-item.active svg,.nav-item:hover svg{opacity:1}
.sdivider{height:1px;background:var(--border);margin:6px 0}
.slabel{padding:8px 20px 3px;font-size:10px;font-weight:700;letter-spacing:1.5px;
  text-transform:uppercase;color:var(--text3)}
.playlists-wrap{flex:1;overflow-y:auto;padding-bottom:8px}
.pl-item{display:flex;align-items:center;gap:10px;padding:7px 20px;
  cursor:pointer;color:var(--text2);transition:color .15s;user-select:none}
.pl-item:hover{color:var(--text)}
.pl-item.active{color:var(--accent2)}
.pl-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.pl-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
.btn-newpl{margin:8px 16px;padding:8px;background:transparent;
  border:1px dashed var(--border2);border-radius:8px;color:var(--text3);cursor:pointer;
  font-size:12px;transition:all .2s;display:flex;align-items:center;gap:6px;
  justify-content:center;width:calc(100% - 32px);font-family:inherit}
.btn-newpl:hover{border-color:var(--accent);color:var(--accent)}

/* ── DARK/LIGHT TOGGLE ── */
.btn-darkmode{background:transparent;border:none;cursor:pointer;padding:4px;
  color:var(--text2);transition:color .2s,transform .3s;display:grid;place-items:center;
  border-radius:6px;flex-shrink:0}
.btn-darkmode:hover{color:var(--text);transform:rotate(20deg)}
.btn-darkmode svg{width:16px;height:16px;display:block}

/* MAIN */
.page{display:none;padding-bottom:32px}
.page.on{display:block}
.hero{padding:40px 32px 24px;background:linear-gradient(180deg,var(--bg4) 0%,var(--bg2) 100%)}
.hero h1{font-size:34px;font-weight:800;letter-spacing:-1px;margin-bottom:4px}
.hero p{color:var(--text2)}
.sec-head{display:flex;justify-content:space-between;align-items:center;padding:22px 32px 14px}
.sec-head h2{font-size:20px;font-weight:700;letter-spacing:-.3px}
.see-all{font-size:12px;color:var(--text3);cursor:pointer;font-weight:600;
  letter-spacing:.3px;transition:color .15s}
.see-all:hover{color:var(--accent2)}

/* CARDS */
.cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:14px;padding:0 32px}
.card{background:var(--surface);border-radius:var(--rl);overflow:hidden;cursor:pointer;
  transition:transform .2s,background .2s;position:relative}
.card:hover{transform:translateY(-3px);background:var(--surface2)}
.card-cov{width:100%;aspect-ratio:1;background:var(--bg4);position:relative;overflow:hidden}
.card-cov img{width:100%;height:100%;object-fit:cover;display:block}
.card-ph{width:100%;height:100%;display:grid;place-items:center;color:var(--text3)}
.card-ph svg{width:38px;height:38px;opacity:.3}
.card-pbtn{position:absolute;bottom:8px;right:8px;width:36px;height:36px;
  background:var(--accent);border-radius:50%;display:grid;place-items:center;
  opacity:0;transform:translateY(5px);transition:all .2s;
  box-shadow:0 4px 20px rgba(124,106,247,.5)}
.card:hover .card-pbtn{opacity:1;transform:translateY(0)}
.card-pbtn svg{width:16px;height:16px;fill:white;margin-left:2px}
.card-info{padding:10px 12px 12px}
.card-ttl{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-sub{font-size:12px;color:var(--text2);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ARTISTS */
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:18px;padding:0 32px}
.acard{cursor:pointer;text-align:center;padding:14px 8px;border-radius:var(--rl);transition:background .2s}
.acard:hover{background:var(--surface)}
.acard-av{width:90px;height:90px;border-radius:50%;background:var(--bg4);margin:0 auto 10px;
  overflow:hidden;display:grid;place-items:center;font-size:26px;font-weight:800;color:white;
  font-family:'Segoe UI',sans-serif;letter-spacing:-.5px}
.acard-av img{width:100%;height:100%;object-fit:cover}

/* ALBUM DETAIL OVERLAY */
#albdetail{display:none;position:absolute;inset:0;background:var(--bg2);z-index:50;overflow-y:auto}
#albdetail.on{display:block}
.adh{display:flex;gap:26px;padding:38px 32px 28px;
  background:linear-gradient(180deg,var(--bg4) 0%,var(--bg2) 100%);align-items:flex-end}
.adh-cov{width:170px;height:170px;border-radius:var(--r);flex-shrink:0;overflow:hidden;
  background:var(--bg4);box-shadow:0 16px 48px rgba(0,0,0,.6)}
.adh-cov img{width:100%;height:100%;object-fit:cover}
.adh-type{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
  color:var(--text2);margin-bottom:7px}
.adh-title{font-size:30px;font-weight:800;letter-spacing:-.8px;margin-bottom:3px}
.adh-artist{color:var(--text2);font-size:14px;margin-bottom:3px}
.tag{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;
  font-weight:600;background:var(--bg4);color:var(--text2);margin-right:4px;margin-top:4px}
.act-row{display:flex;gap:12px;padding:18px 32px;align-items:center}
.btn-play{background:var(--accent);color:white;border:none;padding:11px 26px;border-radius:40px;
  font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:7px;font-family:inherit}
.btn-play:hover{background:var(--accent2);transform:scale(1.03)}
.btn-play svg{width:15px;height:15px;fill:white}
.btn-sec{background:transparent;border:1px solid var(--border2);color:var(--text2);
  padding:9px 16px;border-radius:40px;font-size:13px;cursor:pointer;
  transition:all .2s;display:flex;align-items:center;gap:6px;font-family:inherit}
.btn-sec:hover{background:var(--surface);color:var(--text)}
.btn-sec svg{width:14px;height:14px}

/* TRACKLIST */
.tlist{padding:0 32px 32px}
.tlist-hdr{display:grid;grid-template-columns:40px 1fr 90px 60px;gap:8px;
  padding:6px 14px;color:var(--text3);font-size:11px;font-weight:700;
  letter-spacing:.5px;border-bottom:1px solid var(--border);margin-bottom:2px}
.trow{display:grid;grid-template-columns:40px 1fr 90px 60px;gap:8px;align-items:center;
  padding:7px 14px;border-radius:8px;cursor:pointer;transition:background .15s}
.trow:hover{background:var(--surface)}
.trow.playing{background:var(--accent-dim)}
.trow.playing .trow-title{color:var(--accent2)}
.trow-num{color:var(--text3);font-size:13px;text-align:center;position:relative}
.trow-eq{display:none;align-items:center;justify-content:center;gap:2px;height:16px}
.trow.playing .trow-n{display:none}
.trow.playing .trow-eq{display:flex}
.eq-b{width:3px;background:var(--accent2);border-radius:2px;animation:eqb .6s ease-in-out infinite alternate}
.eq-b:nth-child(2){animation-delay:.1s;height:60%}
.eq-b:nth-child(3){animation-delay:.22s}
.eq-b:nth-child(4){animation-delay:.05s;height:70%}
@keyframes eqb{0%{transform:scaleY(.25)}100%{transform:scaleY(1)}}
.trow-title{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btn-addpl{background:transparent;border:1px solid var(--border);color:var(--text3);
  font-size:11px;padding:4px 9px;border-radius:6px;cursor:pointer;
  transition:all .2s;white-space:nowrap;font-family:inherit}
.btn-addpl:hover{border-color:var(--accent);color:var(--accent)}
.trow-dur{color:var(--text3);font-size:13px;text-align:right}

/* NOW PLAYING PANEL */
.np-hdr{padding:18px 20px 14px;font-size:15px;font-weight:700;
  border-bottom:1px solid var(--border);letter-spacing:-.2px}
.np-covwrap{padding:18px 22px 14px}
.np-cov{width:100%;aspect-ratio:1;border-radius:var(--rl);overflow:hidden;background:var(--bg4);position:relative}
.np-cov img{width:100%;height:100%;object-fit:cover;display:block}
.np-ph{width:100%;height:100%;display:grid;place-items:center;color:var(--text3)}
.np-ph svg{width:50px;height:50px;opacity:.25}
@keyframes pulse-ring{0%,100%{box-shadow:0 0 0 0 rgba(124,106,247,.25)}
  50%{box-shadow:0 0 0 12px rgba(124,106,247,0)}}
.np-cov.anim{animation:pulse-ring 3s ease infinite}
.np-info{padding:0 20px 12px}
.np-title{font-size:16px;font-weight:700;margin-bottom:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:-.2px;
  transition:color .15s}
.np-title:hover{color:var(--accent2)}
.np-artist{font-size:13px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  transition:color .15s}
.np-artist:hover{color:var(--text)}
.np-album{font-size:11px;color:var(--text3);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.np-prog{padding:0 20px 18px}
.prog-outer{height:3px;background:var(--bg4);border-radius:9px;cursor:pointer;
  position:relative;overflow:hidden;margin-bottom:5px}
.prog-inner{height:100%;background:var(--accent);border-radius:9px;transition:width .1s linear}
.prog-times{display:flex;justify-content:space-between;font-size:11px;color:var(--text3)}
.np-queue-hdr{padding:7px 20px;font-size:10px;font-weight:700;letter-spacing:1.5px;
  text-transform:uppercase;color:var(--text3);border-top:1px solid var(--border)}
.np-queue{flex:1;overflow-y:auto;padding-bottom:12px}
.qi{display:flex;gap:10px;align-items:center;padding:7px 20px;cursor:pointer;transition:background .15s}
.qi:hover{background:var(--surface)}
.qi.on{background:var(--accent-dim)}
.qi-cov{width:36px;height:36px;border-radius:6px;background:var(--bg4);flex-shrink:0;overflow:hidden}
.qi-cov img{width:100%;height:100%;object-fit:cover}
.qi-ttl{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.qi-ttl.on{color:var(--accent2)}
.qi-sub{font-size:11px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* PLAYER BAR */
.pb-left{display:flex;align-items:center;gap:14px;width:240px;flex-shrink:0}
.pb-thumb{width:48px;height:48px;border-radius:8px;background:var(--bg4);overflow:hidden;flex-shrink:0}
.pb-thumb img{width:100%;height:100%;object-fit:cover}
.pb-info{min-width:0}
.pb-title{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  transition:color .15s}
.pb-title:hover{color:var(--accent2)}
.pb-artist{font-size:12px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  transition:color .15s}
.pb-artist:hover{color:var(--text)}
.pb-center{flex:1;display:flex;flex-direction:column;align-items:center;gap:7px;min-width:0}
.pb-ctrls{display:flex;align-items:center;gap:14px}
.cbtn{background:transparent;border:none;color:var(--text2);cursor:pointer;
  display:grid;place-items:center;transition:color .15s;padding:4px;border-radius:6px}
.cbtn:hover{color:var(--text)}
.cbtn.on{color:var(--accent)}
.cbtn svg{width:18px;height:18px}
.cbtn.main{width:38px;height:38px;background:var(--text);border-radius:50%;
  color:var(--bg);transition:transform .1s,background .2s}
.cbtn.main:hover{background:var(--accent2);transform:scale(1.07)}
.cbtn.main svg{width:15px;height:15px;fill:var(--bg)}
.pb-prog{display:flex;align-items:center;gap:10px;width:100%;max-width:480px}
.pb-prog span{font-size:11px;color:var(--text3);min-width:34px}
.pb-prog span:last-child{text-align:right}
#seek{flex:1;-webkit-appearance:none;height:3px;background:var(--bg4);
  border-radius:9px;cursor:pointer;outline:none}
#seek::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;
  background:white;border-radius:50%;cursor:pointer;transition:background .2s}
#seek:hover::-webkit-slider-thumb{background:var(--accent2)}
.pb-right{display:flex;align-items:center;gap:10px;width:190px;justify-content:flex-end;flex-shrink:0}
.vbtn{background:transparent;border:none;color:var(--text2);cursor:pointer;padding:4px}
.vbtn:hover{color:var(--text)}
.vbtn svg{width:18px;height:18px;display:block}
#vol{-webkit-appearance:none;width:85px;height:3px;background:var(--bg4);
  border-radius:9px;cursor:pointer;outline:none}
#vol::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;
  background:white;border-radius:50%;cursor:pointer}

/* SEARCH */
.search-wrap{padding:22px 32px 8px}
#sinput{width:100%;background:var(--surface);border:1px solid var(--border);
  color:var(--text);padding:10px 18px;border-radius:40px;font-size:14px;
  outline:none;transition:border-color .2s;font-family:inherit}
#sinput:focus{border-color:var(--accent)}
#sinput::placeholder{color:var(--text3)}
.sres{padding:8px 32px}
.sres-ttl{font-size:18px;font-weight:700;letter-spacing:-.3px;margin:18px 0 10px}
.str{display:flex;align-items:center;gap:14px;padding:7px 10px;border-radius:8px;cursor:pointer;transition:background .15s}
.str:hover{background:var(--surface)}
.str-cov{width:42px;height:42px;border-radius:6px;background:var(--bg4);flex-shrink:0;overflow:hidden}
.str-cov img{width:100%;height:100%;object-fit:cover}

/* PLAYLIST PAGE */
.pl-hero{display:flex;gap:24px;padding:38px 32px 28px;
  background:linear-gradient(180deg,var(--bg4) 0%,var(--bg2) 100%);align-items:flex-end}
.pl-icon{width:170px;height:170px;border-radius:var(--rl);flex-shrink:0;
  display:grid;place-items:center;font-size:60px;overflow:hidden;position:relative;cursor:pointer}
.pl-icon img{width:100%;height:100%;object-fit:cover;position:absolute;inset:0}
.pl-icon-overlay{position:absolute;inset:0;background:rgba(0,0,0,.5);
  display:grid;place-items:center;opacity:0;transition:opacity .2s;border-radius:var(--rl)}
.pl-icon:hover .pl-icon-overlay{opacity:1}
.pl-icon-overlay svg{width:32px;height:32px;fill:white}
.pl-empty{text-align:center;padding:48px 32px;color:var(--text3)}

/* MODAL */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:200;
  display:grid;place-items:center;backdrop-filter:blur(4px)}
.modal-bg.hide{display:none}
.modal{background:var(--surface);border:1px solid var(--border2);border-radius:var(--rl);
  padding:26px;width:350px;max-width:90vw}
.modal h3{font-size:19px;font-weight:700;margin-bottom:18px;letter-spacing:-.3px}
.mfield{margin-bottom:14px}
.mfield label{display:block;font-size:12px;font-weight:700;color:var(--text2);
  margin-bottom:5px;letter-spacing:.3px}
.mfield input{width:100%;background:var(--bg4);border:1px solid var(--border);
  color:var(--text);padding:9px 13px;border-radius:8px;font-size:14px;outline:none;
  transition:border-color .2s;font-family:inherit}
.mfield input:focus{border-color:var(--accent)}
.swatches{display:flex;gap:8px;flex-wrap:wrap}
.swatch{width:26px;height:26px;border-radius:50%;cursor:pointer;
  transition:transform .15s;border:2px solid transparent;flex-shrink:0}
.swatch:hover{transform:scale(1.2)}
.swatch.sel{border-color:white;transform:scale(1.1)}
.mact{display:flex;justify-content:flex-end;gap:10px;margin-top:18px}
.btn-cancel{background:transparent;border:1px solid var(--border2);color:var(--text2);
  padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px;
  transition:background .2s;font-family:inherit}
.btn-cancel:hover{background:var(--bg4)}
.btn-ok{background:var(--accent);border:none;color:white;padding:8px 18px;
  border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;
  transition:background .2s;font-family:inherit}
.btn-ok:hover{background:var(--accent2)}

/* ADD PL LIST */
.apl-item{display:flex;align-items:center;gap:10px;padding:10px 0;cursor:pointer;
  color:var(--text2);transition:color .15s;border-radius:6px}
.apl-item:hover{color:var(--text)}

/* TOAST */
#toast{position:fixed;bottom:100px;left:50%;transform:translateX(-50%);
  background:var(--surface2);border:1px solid var(--border2);color:var(--text);
  padding:9px 20px;border-radius:40px;font-size:13px;z-index:300;
  opacity:0;transition:opacity .25s;pointer-events:none;white-space:nowrap}
#toast.show{opacity:1}
.hide{display:none!important}
</style>
</head>
<body>
<div id="app">

<!-- SIDEBAR -->
<aside id="sidebar">
  <div class="slogo">
    <div class="slogo-mark">
      <svg viewBox="0 0 64 64" width="20" height="20" xmlns="http://www.w3.org/2000/svg">
        <circle cx="32" cy="32" r="28" fill="#1c1c26"/>
        <circle cx="32" cy="32" r="28" fill="none" stroke="#a89eff" stroke-width="1.5" opacity="0.4"/>
        <path d="M 10 13 A 28 28 0 0 0 10 51" fill="none" stroke="#7c6af7" stroke-width="3" stroke-linecap="round"/>
        <path d="M 54 13 A 28 28 0 0 1 54 51" fill="none" stroke="#a89eff" stroke-width="3" stroke-linecap="round"/>
        <polygon points="36,5 24,31 33,31 21,59 44,29 34,29 42,5" fill="white"/>
        <circle cx="32" cy="32" r="3" fill="#a89eff"/>
      </svg>
    </div>
    <span class="slogo-text">Volta</span>
    <button class="btn-darkmode" id="btn-darkmode" onclick="toggleDarkMode()" title="Thème sombre/clair">
      <svg id="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z"/></svg>
      <svg id="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="display:none"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
    </button>
  </div>
  <div class="nav-item active" onclick="pg('home')">
    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg><span data-i18n="home"></span>
  </div>
  <div class="nav-item" onclick="pg('search')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg><span data-i18n="search"></span>
  </div>
  <div class="nav-item" onclick="pg('artists')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg><span data-i18n="artists"></span>
  </div>
  <div class="nav-item" onclick="pg('albums')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg><span data-i18n="albums"></span>
  </div>
  <div class="sdivider"></div>
  <div class="slabel" data-i18n="library"></div>
  <div class="playlists-wrap" id="pl-sidebar"></div>
  <button class="btn-newpl" onclick="openNewPl()">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M12 5v14M5 12h14"/></svg>
    <span data-i18n="new_playlist"></span>
  </button>
</aside>

<!-- MAIN -->
<main id="main">
  <div id="albdetail">
    <div class="adh">
      <div class="adh-cov" id="ad-cov"><div class="np-ph" style="width:100%;height:100%;display:grid;place-items:center"><svg viewBox="0 0 24 24" fill="#4e4c66" width="50" height="50"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div></div>
      <div>
        <div class="adh-type" data-i18n="album_lbl"></div>
        <div class="adh-title" id="ad-ttl"></div>
        <div class="adh-artist" id="ad-art"></div>
        <div style="margin-top:6px"><span class="tag" id="ad-yr"></span><span class="tag" id="ad-gn"></span></div>
      </div>
    </div>
    <div class="act-row">
      <button class="btn-sec" onclick="closeAlb()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5m7-7-7 7 7 7"/></svg><span data-i18n="back"></span>
      </button>
      <button class="btn-play" onclick="playAll()">
        <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg><span data-i18n="play_all"></span>
      </button>
    </div>
    <div class="tlist">
      <div class="tlist-hdr"><span data-i18n="number"></span><span data-i18n="title_col"></span><span>Playlist</span><span style="text-align:right" data-i18n="duration"></span></div>
      <div id="ad-tl"></div>
    </div>
  </div>

  <!-- HOME -->
  <div class="page on" id="page-home">
    <div class="hero"><h1 data-i18n="hello"></h1><p data-i18n="personal_collection"></p></div>
    <div class="sec-head"><h2 data-i18n="recent_albums"></h2><span class="see-all" onclick="pg('albums')" data-i18n="see_all"></span></div>
    <div class="cgrid" id="home-albs"></div>
    <div class="sec-head"><h2 data-i18n="artists"></h2><span class="see-all" onclick="pg('artists')" data-i18n="see_all"></span></div>
    <div class="cgrid" id="home-arts"></div>
  </div>
  <!-- ARTISTS -->
  <div class="page" id="page-artists">
    <div class="hero"><h1 data-i18n="artists"></h1><p data-i18n="all_collection"></p></div>
    <div style="height:14px"></div>
    <div class="agrid" id="arts-grid"></div>
  </div>
  <!-- ALBUMS -->
  <div class="page" id="page-albums">
    <div class="hero"><h1 id="albs-title" data-i18n="albums"></h1></div>
    <div style="height:14px"></div>
    <div class="cgrid" id="albs-grid"></div>
  </div>
  <!-- SEARCH -->
  <div class="page" id="page-search">
    <div class="hero"><h1 data-i18n="search"></h1></div>
    <div class="search-wrap"><input type="text" id="sinput" placeholder="" oninput="doSearch()"></div>
    <div class="sres" id="sres"></div>
  </div>
  <!-- PLAYLIST VIEW -->
  <div class="page" id="page-playlist">
    <div class="pl-hero">
      <div class="pl-icon" id="pl-icon">
        <svg viewBox="0 0 64 64" width="70" height="70" xmlns="http://www.w3.org/2000/svg">
          <circle cx="32" cy="32" r="28" fill="#1c1c26"/>
          <circle cx="32" cy="32" r="28" fill="none" stroke="#a89eff" stroke-width="1.5" opacity="0.4"/>
          <path d="M 10 13 A 28 28 0 0 0 10 51" fill="none" stroke="#7c6af7" stroke-width="3" stroke-linecap="round"/>
          <path d="M 54 13 A 28 28 0 0 1 54 51" fill="none" stroke="#a89eff" stroke-width="3" stroke-linecap="round"/>
          <polygon points="36,5 24,31 33,31 21,59 44,29 34,29 42,5" fill="white"/>
          <circle cx="32" cy="32" r="3" fill="#a89eff"/>
        </svg>
      </div>
      <div>
        <div style="font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--text2);margin-bottom:5px" data-i18n="playlist_lbl"></div>
        <div class="adh-title" id="pl-ttl"></div>
        <div style="color:var(--text2);font-size:14px;margin-top:4px" id="pl-cnt"></div>
      </div>
    </div>
    <div class="act-row">
      <button class="btn-play" onclick="playPl()"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg><span data-i18n="play_all"></span></button>
      <button class="btn-sec" onclick="delPl()" style="color:#e9527c;border-color:rgba(233,82,124,.3)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg><span data-i18n="delete"></span>
      </button>
    </div>
    <div class="tlist">
      <div class="tlist-hdr"><span data-i18n="number"></span><span data-i18n="title_col"></span><span></span><span style="text-align:right" data-i18n="delete"></span></div>
      <div id="pl-tl"></div>
    </div>
  </div>
</main>

<!-- NOW PLAYING PANEL -->
<aside id="nowpanel">
  <div class="np-hdr" data-i18n="now_playing"></div>
  <div class="np-covwrap">
    <div class="np-cov" id="np-cov">
      <div class="np-ph"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div>
    </div>
  </div>
  <div class="np-info">
    <div class="np-title" id="np-ttl"></div>
    <div class="np-artist" id="np-art"></div>
    <div class="np-album" id="np-alb"></div>
  </div>
  <div class="np-prog">
    <div class="prog-outer" id="np-prog-outer" onclick="seekNP(event)">
      <div class="prog-inner" id="np-prog" style="width:0%"></div>
    </div>
    <div class="prog-times"><span id="np-cur">0:00</span><span id="np-dur">0:00</span></div>
  </div>
  <div class="np-queue-hdr" data-i18n="queue"></div>
  <div class="np-queue" id="np-queue"></div>
</aside>

<!-- PLAYER BAR -->
<div id="playerbar">
  <div class="pb-left">
    <div class="pb-thumb" id="pb-thumb">
      <svg viewBox="0 0 48 48" fill="none" style="width:100%;height:100%"><rect width="48" height="48" fill="#1e1e2a"/><path d="M24 14v14.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V18h4v-4h-6z" fill="#4e4c66"/></svg>
    </div>
    <div class="pb-info">
      <div class="pb-title" id="pb-ttl">Volta</div>
      <div class="pb-artist" id="pb-art"></div>
    </div>
  </div>
  <div class="pb-center">
    <div class="pb-ctrls">
      <button class="cbtn" id="btn-shuf" onclick="toggleShuf()" title="Aléatoire">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/></svg>
      </button>
      <button class="cbtn" onclick="prevT()" title="Précédent">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg>
      </button>
      <button class="cbtn main" id="btn-play" onclick="togglePlay()">
        <svg id="play-ico" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <button class="cbtn" onclick="nextT()" title="Suivant">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zm2.5-6L13 15V9l-4.5 3zm7.5 6h2V6h-2v12z"/></svg>
      </button>
      <button class="cbtn" id="btn-rep" onclick="toggleRep()" title="Répéter">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
      </button>
    </div>
    <div class="pb-prog">
      <span id="pb-cur">0:00</span>
      <input type="range" id="seek" min="0" max="100" value="0" step="0.1" oninput="seekTo(this.value)">
      <span id="pb-dur">0:00</span>
    </div>
  </div>
  <div class="pb-right">
    <button class="vbtn" onclick="toggleMute()">
      <svg id="vol-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
        <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
        <path id="vw1" d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
        <path id="vw2" d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
      </svg>
    </button>
    <input type="range" id="vol" min="0" max="100" value="80" oninput="setVol(this.value)">
  </div>
</div>
</div>

<!-- MODAL NEW/EDIT PLAYLIST -->
<div class="modal-bg hide" id="modal-bg" onclick="closeMod(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <h3 id="mod-ttl"></h3>
    <div class="mfield"><label>Nom</label><input type="text" id="mod-name" placeholder=""></div>
    <div class="mfield"><label>Couleur</label><div class="swatches" id="swatches"></div></div>
    <div class="mact">
      <button class="btn-cancel" onclick="closeMod()" data-i18n="cancel"></button>
      <button class="btn-ok" id="mod-ok" data-i18n="create"></button>
    </div>
  </div>
</div>

<!-- MODAL ADD TO PLAYLIST -->
<div class="modal-bg hide" id="apl-bg" onclick="closeApl(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <h3 data-i18n="add_to_playlist"></h3>
    <div id="apl-list" style="max-height:260px;overflow-y:auto;margin-bottom:12px"></div>
    <div class="mact"><button class="btn-cancel" onclick="closeApl()" data-i18n="cancel"></button></div>
  </div>
</div>

<div id="toast"></div>
<audio id="aud" preload="auto"></audio>

<script>
const COLORS=['#7c6af7','#1ed760','#e9527c','#f59e0b','#06b6d4','#f97316','#14b8a6','#ec4899'];
let lib=[],pls={},queue=[],qi=-1,curAlb=null,curPl=null;
let isShuffle=false,isRepeat=false,isMuted=false,prevVol=80;
let selColor=COLORS[0],pendingTrack=null;
const trackReg=new Map();

// ── i18n ──────────────────────────────────────────────────────────────────
const I18N={
  fr:{
    home:`Accueil`,search:`Rechercher`,artists:`Artistes`,albums:`Albums`,
    library:`Bibliothèque`,new_playlist:`Nouvelle playlist`,
    now_playing:`Lecture en cours`,queue:`File d'attente`,
    play_all:`Tout lire`,back:`Retour`,no_track:`Aucune lecture`,
    select_track:`Sélectionnez une chanson`,personal_collection:`Votre collection personnelle`,
    hello:`Bonjour 👋`,recent_albums:`Albums récents`,see_all:`Voir tout`,
    album_lbl:`Album`,add_to_playlist:`Ajouter à une playlist`,
    delete:`Supprimer`,cancel:`Annuler`,create:`Créer`,
    playlist_lbl:`Playlist`,track_count:(n)=>`${n} titre${n!==1?'s':''}`,
    pl_empty:`Ajoutez des morceaux via le bouton + Playlist dans les albums`,
    no_playlist:`Aucune playlist — créez-en une d'abord.`,
    no_playlist_side:`Aucune playlist`,
    added_to:(name)=>`Ajouté à "${name}"`,
    pl_created:(name)=>`Playlist "${name}" créée`,
    pl_deleted:(name)=>`"${name}" supprimée`,
    no_results:(q)=>`Aucun résultat pour « ${q} »`,
    pl_name_ph:`Ma playlist…`,
    color_lbl:`Couleur`,name_lbl:`Nom`,
    duration:`Durée`,number:`#`,title_col:`Titre`,
    shuffle:`Aléatoire`,repeat:`Répéter`,
    all_collection:`Toute votre collection`,
    album_count:(n)=>`${n} album${n>1?'s':''}`,
    music_player:`LECTEUR MUSICAL`,
    loading:`Chargement de votre médiathèque`,
  },
  en:{
    home:`Home`,search:`Search`,artists:`Artists`,albums:`Albums`,
    library:`Library`,new_playlist:`New playlist`,
    now_playing:`Now playing`,queue:`Queue`,
    play_all:`Play all`,back:`Back`,no_track:`Nothing playing`,
    select_track:`Select a song`,personal_collection:`Your personal collection`,
    hello:`Hello 👋`,recent_albums:`Recent albums`,see_all:`See all`,
    album_lbl:`Album`,add_to_playlist:`Add to playlist`,
    delete:`Delete`,cancel:`Cancel`,create:`Create`,
    playlist_lbl:`Playlist`,track_count:(n)=>`${n} track${n!==1?'s':''}`,
    pl_empty:`Add tracks using the + Playlist button in albums`,
    no_playlist:`No playlists — create one first.`,
    no_playlist_side:`No playlists`,
    added_to:(name)=>`Added to "${name}"`,
    pl_created:(name)=>`Playlist "${name}" created`,
    pl_deleted:(name)=>`"${name}" deleted`,
    no_results:(q)=>`No results for "${q}"`,
    pl_name_ph:`My playlist…`,
    color_lbl:`Color`,name_lbl:`Name`,
    duration:`Duration`,number:`#`,title_col:`Title`,
    shuffle:`Shuffle`,repeat:`Repeat`,
    all_collection:`Your full collection`,
    album_count:(n)=>`${n} album${n>1?'s':''}`,
    music_player:`MUSIC PLAYER`,
    loading:`Loading your library`,
  },
  de:{
    home:`Startseite`,search:`Suchen`,artists:`Künstler`,albums:`Alben`,
    library:`Bibliothek`,new_playlist:`Neue Playlist`,
    now_playing:`Läuft gerade`,queue:`Warteschlange`,
    play_all:`Alle abspielen`,back:`Zurück`,no_track:`Nichts läuft`,
    select_track:`Song auswählen`,personal_collection:`Ihre persönliche Sammlung`,
    hello:`Hallo 👋`,recent_albums:`Aktuelle Alben`,see_all:`Alle anzeigen`,
    album_lbl:`Album`,add_to_playlist:`Zur Playlist hinzufügen`,
    delete:`Löschen`,cancel:`Abbrechen`,create:`Erstellen`,
    playlist_lbl:`Playlist`,track_count:(n)=>`${n} Titel`,
    pl_empty:`Füge Titel über + Playlist in den Alben hinzu`,
    no_playlist:`Keine Playlists — erstelle zuerst eine.`,
    no_playlist_side:`Keine Playlists`,
    added_to:(name)=>`Zu "${name}" hinzugefügt`,
    pl_created:(name)=>`Playlist "${name}" erstellt`,
    pl_deleted:(name)=>`"${name}" gelöscht`,
    no_results:(q)=>`Keine Ergebnisse für „${q}"`,
    pl_name_ph:`Meine Playlist…`,
    color_lbl:`Farbe`,name_lbl:`Name`,
    duration:`Dauer`,number:`#`,title_col:`Titel`,
    shuffle:`Zufällig`,repeat:`Wiederholen`,
    all_collection:`Ihre gesamte Sammlung`,
    album_count:(n)=>`${n} Album${n>1?'s':''}`,
    music_player:`MUSIKPLAYER`,
    loading:`Bibliothek wird geladen`,
  },
  es:{
    home:`Inicio`,search:`Buscar`,artists:`Artistas`,albums:`Álbumes`,
    library:`Biblioteca`,new_playlist:`Nueva playlist`,
    now_playing:`Reproduciendo`,queue:`Cola`,
    play_all:`Reproducir todo`,back:`Volver`,no_track:`Sin reproducción`,
    select_track:`Selecciona una canción`,personal_collection:`Tu colección personal`,
    hello:`Hola 👋`,recent_albums:`Álbumes recientes`,see_all:`Ver todo`,
    album_lbl:`Álbum`,add_to_playlist:`Añadir a playlist`,
    delete:`Eliminar`,cancel:`Cancelar`,create:`Crear`,
    playlist_lbl:`Playlist`,track_count:(n)=>`${n} pista${n!==1?'s':''}`,
    pl_empty:`Añade canciones con el botón + Playlist en los álbumes`,
    no_playlist:`Sin playlists — crea una primero.`,
    no_playlist_side:`Sin playlists`,
    added_to:(name)=>`Añadido a "${name}"`,
    pl_created:(name)=>`Playlist "${name}" creada`,
    pl_deleted:(name)=>`"${name}" eliminada`,
    no_results:(q)=>`Sin resultados para "${q}"`,
    pl_name_ph:`Mi playlist…`,
    color_lbl:`Color`,name_lbl:`Nombre`,
    duration:`Duración`,number:`#`,title_col:`Título`,
    shuffle:`Aleatorio`,repeat:`Repetir`,
    all_collection:`Toda tu colección`,
    album_count:(n)=>`${n} álbum${n>1?'es':''}`,
    music_player:`REPRODUCTOR`,
    loading:`Cargando tu biblioteca`,
  },
  it:{
    home:`Home`,search:`Cerca`,artists:`Artisti`,albums:`Album`,
    library:`Libreria`,new_playlist:`Nuova playlist`,
    now_playing:`In riproduzione`,queue:`Coda`,
    play_all:`Riproduci tutto`,back:`Indietro`,no_track:`Nessuna riproduzione`,
    select_track:`Seleziona un brano`,personal_collection:`La tua collezione`,
    hello:`Ciao 👋`,recent_albums:`Album recenti`,see_all:`Vedi tutto`,
    album_lbl:`Album`,add_to_playlist:`Aggiungi a playlist`,
    delete:`Elimina`,cancel:`Annulla`,create:`Crea`,
    playlist_lbl:`Playlist`,track_count:(n)=>`${n} brano${n!==1?'i':''}`,
    pl_empty:`Aggiungi brani con il pulsante + Playlist negli album`,
    no_playlist:`Nessuna playlist — creane una prima.`,
    no_playlist_side:`Nessuna playlist`,
    added_to:(name)=>`Aggiunto a "${name}"`,
    pl_created:(name)=>`Playlist "${name}" creata`,
    pl_deleted:(name)=>`"${name}" eliminata`,
    no_results:(q)=>`Nessun risultato per "${q}"`,
    pl_name_ph:`La mia playlist…`,
    color_lbl:`Colore`,name_lbl:`Nome`,
    duration:`Durata`,number:`#`,title_col:`Titolo`,
    shuffle:`Casuale`,repeat:`Ripeti`,
    all_collection:`Tutta la tua collezione`,
    album_count:(n)=>`${n} album`,
    music_player:`LETTORE MUSICALE`,
    loading:`Caricamento della libreria`,
  },
  pt:{
    home:`Início`,search:`Pesquisar`,artists:`Artistas`,albums:`Álbuns`,
    library:`Biblioteca`,new_playlist:`Nova playlist`,
    now_playing:`A tocar`,queue:`Fila`,
    play_all:`Tocar tudo`,back:`Voltar`,no_track:`Nada a tocar`,
    select_track:`Selecione uma música`,personal_collection:`Sua coleção pessoal`,
    hello:`Olá 👋`,recent_albums:`Álbuns recentes`,see_all:`Ver tudo`,
    album_lbl:`Álbum`,add_to_playlist:`Adicionar à playlist`,
    delete:`Eliminar`,cancel:`Cancelar`,create:`Criar`,
    playlist_lbl:`Playlist`,track_count:(n)=>`${n} faixa${n!==1?'s':''}`,
    pl_empty:`Adicione músicas com o botão + Playlist nos álbuns`,
    no_playlist:`Sem playlists — crie uma primeiro.`,
    no_playlist_side:`Sem playlists`,
    added_to:(name)=>`Adicionado a "${name}"`,
    pl_created:(name)=>`Playlist "${name}" criada`,
    pl_deleted:(name)=>`"${name}" eliminada`,
    no_results:(q)=>`Sem resultados para "${q}"`,
    pl_name_ph:`Minha playlist…`,
    color_lbl:`Cor`,name_lbl:`Nome`,
    duration:`Duração`,number:`#`,title_col:`Título`,
    shuffle:`Aleatório`,repeat:`Repetir`,
    all_collection:`Toda a sua coleção`,
    album_count:(n)=>`${n} álbum${n>1?'ns':''}`,
    music_player:`LEITOR DE MÚSICA`,
    loading:`A carregar a sua biblioteca`,
  },
  nl:{
    home:`Startpagina`,search:`Zoeken`,artists:`Artiesten`,albums:`Albums`,
    library:`Bibliotheek`,new_playlist:`Nieuwe afspeellijst`,
    now_playing:`Nu afspelen`,queue:`Wachtrij`,
    play_all:`Alles afspelen`,back:`Terug`,no_track:`Niets speelt`,
    select_track:`Kies een nummer`,personal_collection:`Uw persoonlijke collectie`,
    hello:`Hallo 👋`,recent_albums:`Recente albums`,see_all:`Alles zien`,
    album_lbl:`Album`,add_to_playlist:`Aan afspeellijst toevoegen`,
    delete:`Verwijderen`,cancel:`Annuleren`,create:`Maken`,
    playlist_lbl:`Afspeellijst`,track_count:(n)=>`${n} nummer${n!==1?'s':''}`,
    pl_empty:`Voeg nummers toe via + Playlist in albums`,
    no_playlist:`Geen afspeellijsten — maak er eerst een.`,
    no_playlist_side:`Geen afspeellijsten`,
    added_to:(name)=>`Toegevoegd aan "${name}"`,
    pl_created:(name)=>`Afspeellijst "${name}" gemaakt`,
    pl_deleted:(name)=>`"${name}" verwijderd`,
    no_results:(q)=>`Geen resultaten voor "${q}"`,
    pl_name_ph:`Mijn afspeellijst…`,
    color_lbl:`Kleur`,name_lbl:`Naam`,
    duration:`Duur`,number:`#`,title_col:`Titel`,
    shuffle:`Willekeurig`,repeat:`Herhalen`,
    all_collection:`Uw volledige collectie`,
    album_count:(n)=>`${n} album${n>1?'s':''}`,
    music_player:`MUZIEKSPELER`,
    loading:`Bibliotheek laden`,
  },
  pl:{
    home:`Strona główna`,search:`Szukaj`,artists:`Artyści`,albums:`Albumy`,
    library:`Biblioteka`,new_playlist:`Nowa playlista`,
    now_playing:`Teraz gra`,queue:`Kolejka`,
    play_all:`Odtwórz wszystko`,back:`Wróć`,no_track:`Nic nie gra`,
    select_track:`Wybierz utwór`,personal_collection:`Twoja kolekcja`,
    hello:`Cześć 👋`,recent_albums:`Ostatnie albumy`,see_all:`Zobacz wszystko`,
    album_lbl:`Album`,add_to_playlist:`Dodaj do playlisty`,
    delete:`Usuń`,cancel:`Anuluj`,create:`Utwórz`,
    playlist_lbl:`Playlista`,track_count:(n)=>`${n} utwór${n>1?'y':''}`,
    pl_empty:`Dodaj utwory przyciskiem + Playlist w albumach`,
    no_playlist:`Brak playlist — najpierw utwórz jedną.`,
    no_playlist_side:`Brak playlist`,
    added_to:(name)=>`Dodano do "${name}"`,
    pl_created:(name)=>`Playlista "${name}" utworzona`,
    pl_deleted:(name)=>`"${name}" usunięta`,
    no_results:(q)=>`Brak wyników dla "${q}"`,
    pl_name_ph:`Moja playlista…`,
    color_lbl:`Kolor`,name_lbl:`Nazwa`,
    duration:`Czas`,number:`#`,title_col:`Tytuł`,
    shuffle:`Losowo`,repeat:`Powtarzaj`,
    all_collection:`Cała Twoja kolekcja`,
    album_count:(n)=>`${n} album${n>1?'ów':''}`,
    music_player:`ODTWARZACZ`,
    loading:`Ładowanie biblioteki`,
  },
  ru:{
    home:`Главная`,search:`Поиск`,artists:`Исполнители`,albums:`Альбомы`,
    library:`Библиотека`,new_playlist:`Новый плейлист`,
    now_playing:`Сейчас играет`,queue:`Очередь`,
    play_all:`Играть всё`,back:`Назад`,no_track:`Ничего не играет`,
    select_track:`Выберите песню`,personal_collection:`Ваша коллекция`,
    hello:`Привет 👋`,recent_albums:`Последние альбомы`,see_all:`Смотреть всё`,
    album_lbl:`Альбом`,add_to_playlist:`Добавить в плейлист`,
    delete:`Удалить`,cancel:`Отмена`,create:`Создать`,
    playlist_lbl:`Плейлист`,track_count:(n)=>`${n} трек${n>1?'а':''}`,
    pl_empty:`Добавляйте треки кнопкой + Playlist в альбомах`,
    no_playlist:`Нет плейлистов — создайте один.`,
    no_playlist_side:`Нет плейлистов`,
    added_to:(name)=>`Добавлено в "${name}"`,
    pl_created:(name)=>`Плейлист "${name}" создан`,
    pl_deleted:(name)=>`"${name}" удалён`,
    no_results:(q)=>`Нет результатов для «${q}»`,
    pl_name_ph:`Мой плейлист…`,
    color_lbl:`Цвет`,name_lbl:`Название`,
    duration:`Длит.`,number:`#`,title_col:`Название`,
    shuffle:`Случайно`,repeat:`Повтор`,
    all_collection:`Вся ваша коллекция`,
    album_count:(n)=>`${n} альбом${n>1?'а':''}`,
    music_player:`МУЗЫКАЛЬНЫЙ ПЛЕЕР`,
    loading:`Загрузка библиотеки`,
  },
}
let T=I18N.en; // sera remplacé après détection
function t(key,...args){
  const v=T[key];
  if(typeof v==='function') return v(...args);
  return v||key;
}
const aud=document.getElementById('aud');

// ── DARK / LIGHT MODE ────────────────────────────────────────
const DARK_PALETTE = {
  bg:'#0a0a0f',bg2:'#111118',bg3:'#18181f',bg4:'#1e1e2a',
  surface:'#1c1c26',surface2:'#252535',
  text:'#f0efff',text2:'#9896b8',text3:'#4e4c66',
  border:'rgba(255,255,255,.06)',border2:'rgba(255,255,255,.13)',
  accent:'#7c6af7',accent2:'#a89eff',dim:'rgba(124,106,247,.18)',
};
const LIGHT_PALETTE = {
  bg:'#f5f5f8',bg2:'#ffffff',bg3:'#ebebf0',bg4:'#dddde8',
  surface:'#eeeef5',surface2:'#e4e4ef',
  text:'#0a0a1a',text2:'#4a4a6a',text3:'#9090b0',
  border:'rgba(0,0,0,.07)',border2:'rgba(0,0,0,.14)',
  accent:'#7c6af7',accent2:'#a89eff',dim:'rgba(124,106,247,.18)',
};

let isDark = true;

function applyPalette(dark){
  isDark = dark;
  const p = dark ? DARK_PALETTE : LIGHT_PALETTE;
  const s = document.documentElement.style;
  s.setProperty('--bg',       p.bg);
  s.setProperty('--bg2',      p.bg2);
  s.setProperty('--bg3',      p.bg3);
  s.setProperty('--bg4',      p.bg4);
  s.setProperty('--surface',  p.surface);
  s.setProperty('--surface2', p.surface2);
  s.setProperty('--text',     p.text);
  s.setProperty('--text2',    p.text2);
  s.setProperty('--text3',    p.text3);
  s.setProperty('--border',   p.border);
  s.setProperty('--border2',  p.border2);
  s.setProperty('--accent',   p.accent);
  s.setProperty('--accent2',  p.accent2);
  s.setProperty('--accent-dim',p.dim);
  // Icônes lune/soleil
  document.getElementById('icon-moon').style.display = dark  ? 'block' : 'none';
  document.getElementById('icon-sun').style.display  = dark  ? 'none'  : 'block';
  // Sauvegarde via Flask
  fetch('/api/theme',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dark})}).catch(()=>{});
}

function toggleDarkMode(){
  applyPalette(!isDark);
}

async function restoreTheme(){
  try{
    const r = await fetch('/api/theme');
    const d = await r.json();
    applyPalette(d.dark !== false); // défaut : sombre
  }catch(e){
    applyPalette(true);
  }
}

// ── INIT ──────────────────────────────────────────────────────
function applyLang(){
  // 1. Éléments avec data-i18n (texte)
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    const key=el.getAttribute('data-i18n');
    const val=t(key);
    if(val) el.textContent=val;
  });
  // 2. Placeholders
  const si=document.getElementById('sinput');
  if(si) si.placeholder=t('search')+' …';
  const mn=document.getElementById('mod-name');
  if(mn) mn.placeholder=t('pl_name_ph');
  // 3. Textes dynamiques initiaux
  document.getElementById('np-ttl').textContent=t('no_track');
  document.getElementById('np-art').textContent=t('select_track');
  document.getElementById('pb-art').textContent=t('no_track');
  // 4. Titre albums page
  document.getElementById('albs-title').textContent=t('albums');
}

buildSwatches();
restoreTheme();
(async()=>{
  // Détecte la langue système via Python
  try{
    const r=await fetch('/api/lang');
    const d=await r.json();
    T=I18N[d.lang]||I18N.en;
  }catch(e){ T=I18N.en; }
  applyLang();
  await Promise.all([loadLib(),loadPls()]);
})();

async function loadLib(){
  const r=await fetch('/api/library');lib=await r.json();renderAll();
}
async function loadPls(){
  const r=await fetch('/api/playlists');pls=await r.json();renderPlSidebar();
}

// ── RENDER ────────────────────────────────────────────────────
function renderAll(){renderHome();renderArts();renderAlbs();}

function covUrl(alb){return alb.has_cover?`/api/cover/${alb.path}`:null;}
function covImg(url){
  return url?`<img src="${url}" alt="" loading="lazy">`
    :`<div class="card-ph"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div>`;
}
function albCard(al){
  const u=covUrl(al);
  return `<div class="card" onclick="openAlb('${x(al.id)}')">
    <div class="card-cov">${covImg(u)}<div class="card-pbtn"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div></div>
    <div class="card-info"><div class="card-ttl">${x(al.name)}</div><div class="card-sub">${x(al.artist||'')}${al.year?' · '+al.year:''}</div></div>
  </div>`;
}
function renderHome(){
  const albs=lib.flatMap(a=>a.albums);
  document.getElementById('home-albs').innerHTML=albs.slice(0,12).map(albCard).join('');
  document.getElementById('home-arts').innerHTML=lib.slice(0,8).map(a=>{
    const img=artistCovUrl(a);
    const letter=a.name.replace(/^The\s+/i,'')[0].toUpperCase();
    const [bg,fg]=avatarColor(a.name);
    const placeholder=`<div style="width:100%;height:100%;display:grid;place-items:center;background:${bg};color:${fg};font-size:42px;font-weight:800;font-family:'Segoe UI',sans-serif">${letter}</div>`;
    return `<div class="card" onclick="showArtist('${x(a.name)}')">
      <div class="card-cov">${img?covImg(img):placeholder}<div class="card-pbtn"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div></div>
      <div class="card-info"><div class="card-ttl">${x(a.name)}</div><div class="card-sub">${a.albums.length} ${t('album_count',a.albums.length)}</div></div>
    </div>`;
  }).join('');
}
const AVATAR_COLORS=[
  ['#5b4fcf','#8b7ff7'],['#1a6b4a','#2dba81'],['#8b3a62','#d96fa0'],
  ['#4a5e8b','#7b9edb'],['#7a4a1a','#db8b3a'],['#3a6b6b','#5dbdbd'],
  ['#6b3a8b','#b87adb'],['#1a5c6b','#2fa8bd'],['#6b5a1a','#c9a83a'],
  ['#1a3a6b','#3a7ac9']
];
function avatarColor(name){
  let h=0;for(let i=0;i<name.length;i++)h=(h*31+name.charCodeAt(i))>>>0;
  return AVATAR_COLORS[h%AVATAR_COLORS.length];
}
function artistCovUrl(a){
  if(a.cover_type==='artist') return `/api/artist_cover/${encodeURIComponent(a.name)}`;
  if(a.cover_type==='album'&&a.cover_path) return `/api/cover/${a.cover_path}`;
  return null;
}
function renderArts(){
  document.getElementById('arts-grid').innerHTML=lib.map(a=>{
    const img=artistCovUrl(a);
    const letter=a.name.replace(/^The\s+/i,'')[0].toUpperCase();
    const [bg,fg]=avatarColor(a.name);
    return `<div class="acard" onclick="showArtist('${x(a.name)}')">
      <div class="acard-av" style="${img?'':'background:'+bg+';color:'+fg}">${img?`<img src="${img}" alt="" style="width:100%;height:100%;object-fit:cover">`:letter}</div>
      <div style="font-weight:600;font-size:13px">${x(a.name)}</div>
      <div style="font-size:11px;color:var(--text2);margin-top:2px">${a.albums.length} ${t('album_count',a.albums.length)}</div>
    </div>`;
  }).join('');
}
function renderAlbs(arr){
  const list=arr||lib.flatMap(a=>a.albums);
  document.getElementById('albs-grid').innerHTML=list.map(albCard).join('');
}
function renderPlSidebar(){
  const el=document.getElementById('pl-sidebar');
  const keys=Object.keys(pls);
  el.innerHTML=keys.length?keys.map(id=>`
    <div class="pl-item${curPl===id?' active':''}" onclick="showPl('${id}')">
      <div class="pl-dot" style="background:${pls[id].color}"></div>
      <div class="pl-name">${x(pls[id].name)}</div>
    </div>`).join('')
    :`<div style="padding:10px 20px;color:var(--text3);font-size:12px">${t('no_playlist_side')}</div>`;
}

// ── NAVIGATION ────────────────────────────────────────────────
function pg(name){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('on');
  const map={home:0,search:1,artists:2,albums:3};
  const ni=document.querySelectorAll('.nav-item');
  if(map[name]!==undefined)ni[map[name]].classList.add('active');
  closeAlb();
  if(name!=='playlist')curPl=null;
  renderPlSidebar();
  document.getElementById('albs-title').textContent=t('albums');
}
function showArtist(name){
  const a=lib.find(a=>a.name===name);if(!a)return;
  renderAlbs(a.albums);
  document.getElementById('albs-title').textContent=name;
  pg('albums');
}

// ── ALBUM DETAIL ──────────────────────────────────────────────
function openAlb(id){
  const alb=lib.flatMap(a=>a.albums).find(a=>a.id===id);if(!alb)return;
  curAlb=alb;
  const u=covUrl(alb);
  document.getElementById('ad-cov').innerHTML=u?`<img src="${u}" alt="" style="width:100%;height:100%;object-fit:cover">`
    :`<div class="np-ph" style="width:100%;height:100%;display:grid;place-items:center"><svg viewBox="0 0 24 24" fill="#4e4c66" width="50" height="50"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div>`;
  document.getElementById('ad-ttl').textContent=alb.name;
  document.getElementById('ad-art').textContent=alb.artist||alb.id.split('|')[0];
  document.getElementById('ad-yr').textContent=alb.year||'';
  document.getElementById('ad-gn').textContent=alb.genre||'';
  renderTlist();
  document.getElementById('albdetail').classList.add('on');
}
function closeAlb(){document.getElementById('albdetail').classList.remove('on');}

function renderTlist(){
  if(!curAlb)return;
  const u=covUrl(curAlb)||'';
  // Stocke les objets track dans le registre pour éviter tout problème d'échappement
  document.getElementById('ad-tl').innerHTML=curAlb.tracks.map((trk,i)=>{
    const fn=trk.filename||trk.file||'';
    const ttl=trk.title||fn.replace(/\.[^/.]+$/,'');
    const dur=trk.duration||'';
    const playing=isCurTrack(curAlb,i);
    const regKey=`alb_${i}`;
    trackReg.set(regKey,{title:ttl,artist:curAlb.artist||'',albumName:curAlb.name,albumPath:curAlb.path,coverUrl:u,filename:fn,trackIdx:i});
    return `<div class="trow${playing?' playing':''}" onclick="playAlbFrom(${i})">
      <div class="trow-num"><span class="trow-n">${i+1}</span><div class="trow-eq"><div class="eq-b"></div><div class="eq-b"></div><div class="eq-b"></div><div class="eq-b"></div></div></div>
      <div class="trow-title">${x(ttl)}</div>
      <button class="btn-addpl" onclick="event.stopPropagation();openApl('${regKey}')">+ ${t('playlist_lbl')}</button>
      <div class="trow-dur">${fmtDur(dur)}</div>
    </div>`;
  }).join('');
}
function isCurTrack(alb,i){
  if(qi<0||!queue[qi])return false;
  return queue[qi].albumPath===alb.path&&queue[qi].trackIdx===i;
}

// ── PLAYBACK ──────────────────────────────────────────────────
function buildQ(alb){
  const u=covUrl(alb)||'';
  return alb.tracks.map((trk,i)=>({
    title:trk.title||(trk.filename||'').replace(/\.[^/.]+$/,''),
    artist:alb.artist||alb.id.split('|')[0],
    albumName:alb.name,albumPath:alb.path,
    coverUrl:u,filename:trk.filename||trk.file||'',trackIdx:i,
  }));
}
function playAll(){if(!curAlb)return;queue=buildQ(curAlb);qi=0;playT();}
function playAlbFrom(i){if(!curAlb)return;queue=buildQ(curAlb);qi=i;playT();}
function playPl(){
  if(!curPl||!pls[curPl])return;
  queue=[...(pls[curPl].tracks||[])];qi=0;playT();
}
function playPlFrom(i){
  if(!curPl)return;queue=[...(pls[curPl].tracks||[])];qi=i;playT();
}

function playT(){
  if(qi<0||qi>=queue.length)return;
  const t=queue[qi];
  const pathEnc=t.albumPath.split('/').map(encodeURIComponent).join('/');
  const fileEnc=encodeURIComponent(t.filename);
  aud.src=`/api/track/${pathEnc}/${fileEnc}`;
  aud.volume=document.getElementById('vol').value/100;
  aud.play().catch(e=>console.error(e));
  updateNP(t);renderQueue();
  if(curAlb&&t.albumPath===curAlb.path)renderTlist();
  if(curPl)renderPlTlist();
}
function updateNP(t){
  // Panneau Now Playing — titre clique → album, artiste clique → page artiste
  const npTtl=document.getElementById('np-ttl');
  npTtl.textContent=t.title||'Inconnu';
  npTtl.style.cursor=t.albumPath?'pointer':'default';
  npTtl.onclick=t.albumPath?()=>goToAlbum(t):null;

  const npArt=document.getElementById('np-art');
  npArt.textContent=t.artist||'';
  npArt.style.cursor=t.artist?'pointer':'default';
  npArt.onclick=t.artist?()=>goToArtist(t.artist):null;

  document.getElementById('np-alb').textContent=t.albumName||'';

  // Barre player bas — idem
  const pbTtl=document.getElementById('pb-ttl');
  pbTtl.textContent=t.title||'Inconnu';
  pbTtl.style.cursor=t.albumPath?'pointer':'default';
  pbTtl.onclick=t.albumPath?()=>goToAlbum(t):null;

  const pbArt=document.getElementById('pb-art');
  pbArt.textContent=t.artist||'';
  pbArt.style.cursor=t.artist?'pointer':'default';
  pbArt.onclick=t.artist?()=>goToArtist(t.artist):null;
  const nc=document.getElementById('np-cov');
  nc.classList.remove('anim');
  nc.innerHTML=t.coverUrl?`<img src="${t.coverUrl}" alt="" style="width:100%;height:100%;object-fit:cover;display:block">`
    :`<div class="np-ph"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div>`;
  document.getElementById('pb-thumb').innerHTML=t.coverUrl
    ?`<img src="${t.coverUrl}" style="width:100%;height:100%;object-fit:cover" alt="">`
    :`<svg viewBox="0 0 48 48" fill="none" style="width:100%;height:100%"><rect width="48" height="48" fill="#1e1e2a"/><path d="M24 14v14.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V18h4v-4h-6z" fill="#4e4c66"/></svg>`;
}
function goToAlbum(t){
  // Retrouve l'album depuis albumPath et l'ouvre
  const alb=lib.flatMap(a=>a.albums).find(a=>a.path===t.albumPath);
  if(!alb)return;
  // Assure qu'on est sur une page visible avant d'ouvrir l'overlay
  if(!document.getElementById('page-home').classList.contains('on')&&
     !document.getElementById('page-albums').classList.contains('on')&&
     !document.getElementById('page-artists').classList.contains('on')&&
     !document.getElementById('page-search').classList.contains('on')&&
     !document.getElementById('page-playlist').classList.contains('on')){
    pg('home');
  }
  openAlb(alb.id);
}

function goToArtist(artistName){
  showArtist(artistName);
}

function renderQueue(){
  document.getElementById('np-queue').innerHTML=queue.map((trk,i)=>`
    <div class="qi${i===qi?' on':''}" onclick="qi=${i};playT()">
      <div class="qi-cov">${trk.coverUrl?`<img src="${trk.coverUrl}" alt="">`:''}</div>
      <div><div class="qi-ttl${i===qi?' on':''}">${x(trk.title)}</div><div class="qi-sub">${x(trk.artist)}</div></div>
    </div>`).join('');
}

function togglePlay(){
  if(aud.paused){if(aud.src)aud.play();}else aud.pause();
}
function nextT(){
  if(!queue.length)return;
  qi=isShuffle?Math.floor(Math.random()*queue.length):(qi+1)%queue.length;
  playT();
}
function prevT(){
  if(aud.currentTime>3){aud.currentTime=0;return;}
  qi=Math.max(0,qi-1);playT();
}
function toggleShuf(){isShuffle=!isShuffle;document.getElementById('btn-shuf').classList.toggle('on',isShuffle);}
function toggleRep(){isRepeat=!isRepeat;document.getElementById('btn-rep').classList.toggle('on',isRepeat);}
function seekTo(v){if(!aud.duration)return;aud.currentTime=(v/100)*aud.duration;}
function seekNP(e){
  if(!aud.duration)return;
  const r=e.currentTarget.getBoundingClientRect();
  aud.currentTime=((e.clientX-r.left)/r.width)*aud.duration;
}
function setVol(v){aud.volume=v/100;isMuted=v==0;updVolIco(v);}
function toggleMute(){
  if(isMuted){aud.volume=prevVol/100;document.getElementById('vol').value=prevVol;updVolIco(prevVol);}
  else{prevVol=document.getElementById('vol').value;aud.volume=0;document.getElementById('vol').value=0;updVolIco(0);}
  isMuted=!isMuted;
}
function updVolIco(v){
  document.getElementById('vw1').style.display=v==0?'none':'';
  document.getElementById('vw2').style.display=v<50?'none':'';
}

aud.addEventListener('play',()=>{
  document.getElementById('play-ico').innerHTML='<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>';
  document.getElementById('np-cov').classList.add('anim');
});
aud.addEventListener('pause',()=>{
  document.getElementById('play-ico').innerHTML='<path d="M8 5v14l11-7z"/>';
  document.getElementById('np-cov').classList.remove('anim');
});
aud.addEventListener('ended',()=>{
  if(isRepeat){aud.currentTime=0;aud.play();return;}
  nextT();
});
aud.addEventListener('timeupdate',()=>{
  if(!aud.duration)return;
  const pct=(aud.currentTime/aud.duration)*100;
  document.getElementById('seek').value=pct;
  document.getElementById('np-prog').style.width=pct+'%';
  document.getElementById('pb-cur').textContent=ft(aud.currentTime);
  document.getElementById('np-cur').textContent=ft(aud.currentTime);
});
aud.addEventListener('loadedmetadata',()=>{
  document.getElementById('pb-dur').textContent=ft(aud.duration);
  document.getElementById('np-dur').textContent=ft(aud.duration);
});

// ── PLAYLISTS ─────────────────────────────────────────────────
function openNewPl(){
  document.getElementById('mod-ttl').textContent=t('new_playlist');
  document.getElementById('mod-ok').textContent=t('create');
  document.getElementById('mod-name').value='';
  selColor=COLORS[0];updSwatches();
  document.getElementById('mod-ok').onclick=confirmNewPl;
  document.getElementById('modal-bg').classList.remove('hide');
  setTimeout(()=>document.getElementById('mod-name').focus(),60);
}
async function confirmNewPl(){
  const name=document.getElementById('mod-name').value.trim();
  if(!name)return;
  const r=await fetch('/api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,color:selColor})});
  const pl=await r.json();
  pls[pl.id]=pl;renderPlSidebar();closeMod();toast(`Playlist "${name}" créée`);
}
function showPl(id){
  curPl=id;const pl=pls[id];if(!pl)return;
  document.getElementById('pl-ttl').textContent=pl.name;
  const n=(pl.tracks||[]).length;
  document.getElementById('pl-cnt').textContent=t('track_count',n);
  const ico=document.getElementById('pl-icon');
  // Overlay cliquable pour changer la cover
  ico.onclick=()=>pickPlCover(id);
  if(pl.cover_file){
    // Affiche la cover uploadée
    ico.innerHTML=`
      <img src="/api/playlist_cover/${id}?_=${Date.now()}" alt="">
      <div class="pl-icon-overlay">
        <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
      </div>`;
  } else {
    ico.style.background=pl.color+'22';ico.style.color=pl.color;
    ico.innerHTML=`
      <svg viewBox="0 0 64 64" width="70" height="70" xmlns="http://www.w3.org/2000/svg">
        <circle cx="32" cy="32" r="28" fill="#1c1c26"/>
        <circle cx="32" cy="32" r="28" fill="none" stroke="#a89eff" stroke-width="1.5" opacity="0.4"/>
        <path d="M 10 13 A 28 28 0 0 0 10 51" fill="none" stroke="#7c6af7" stroke-width="3" stroke-linecap="round"/>
        <path d="M 54 13 A 28 28 0 0 1 54 51" fill="none" stroke="#a89eff" stroke-width="3" stroke-linecap="round"/>
        <polygon points="36,5 24,31 33,31 21,59 44,29 34,29 42,5" fill="white"/>
        <circle cx="32" cy="32" r="3" fill="#a89eff"/>
      </svg>
      <div class="pl-icon-overlay">
        <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
      </div>`;
  }
  renderPlTlist();pg('playlist');renderPlSidebar();
}

function pickPlCover(pid){
  const inp=document.createElement('input');
  inp.type='file';
  inp.accept='image/*';
  inp.onchange=async()=>{
    const file=inp.files[0];if(!file)return;
    const ext=file.name.split('.').pop().toLowerCase();
    const reader=new FileReader();
    reader.onload=async(e)=>{
      const b64=e.target.result.split(',')[1];
      const r=await fetch(`/api/playlist_cover/${pid}`,{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({image_b64:b64,ext})
      });
      const d=await r.json();
      if(d.ok){
        pls[pid].cover_file=d.cover_file;
        showPl(pid);
        toast('Cover mise à jour');
      }
    };
    reader.readAsDataURL(file);
  };
  inp.click();
}
function renderPlTlist(){
  const pl=pls[curPl];if(!pl)return;
  const el=document.getElementById('pl-tl');
  if(!pl.tracks||!pl.tracks.length){
    el.innerHTML=`<div class="pl-empty">${t('pl_empty')}</div>`;return;
  }
  el.innerHTML=pl.tracks.map((trk,i)=>`
    <div class="trow${qi>=0&&queue[qi]&&queue[qi].albumPath===t.albumPath&&queue[qi].trackIdx===t.trackIdx?' playing':''}" onclick="playPlFrom(${i})">
      <div class="trow-num"><span class="trow-n">${i+1}</span><div class="trow-eq"><div class="eq-b"></div><div class="eq-b"></div><div class="eq-b"></div><div class="eq-b"></div></div></div>
      <div class="trow-title">${x(trk.title)}</div>
      <div></div>
      <button class="btn-addpl" style="color:#e9527c;border-color:rgba(233,82,124,.3)" onclick="event.stopPropagation();remFromPl(${i})">✕</button>
    </div>`).join('');
}
async function remFromPl(i){
  const pl=pls[curPl];pl.tracks.splice(i,1);
  await savePl(curPl,pl);showPl(curPl);
}
async function delPl(){
  if(!curPl)return;
  const name=pls[curPl].name;
  await fetch(`/api/playlists/${curPl}`,{method:'DELETE'});
  delete pls[curPl];curPl=null;renderPlSidebar();pg('home');toast(t('pl_deleted',name));
}

// Hint visuel : tooltip sur l'icône playlist
document.getElementById('pl-icon').title='Cliquer pour changer la cover';
function openApl(key){
  // key est soit une clé du registre trackReg, soit un objet direct
  if(typeof key==='string'&&trackReg.has(key)){
    pendingTrack=trackReg.get(key);
  } else if(typeof key==='object'&&key!==null){
    pendingTrack=key;
  } else {
    console.error('openApl: clé inconnue',key); return;
  }
  document.querySelector('#apl-bg h3').textContent=t('add_to_playlist');
  const keys=Object.keys(pls);
  const el=document.getElementById('apl-list');
  el.innerHTML=keys.length?keys.map(id=>`
    <div class="apl-item" onclick="addToPl('${id}')">
      <div class="pl-dot" style="background:${pls[id].color}"></div>
      <div style="font-size:14px">${x(pls[id].name)}</div>
    </div>`).join('')
    :`<div style="color:var(--text2);padding:12px 0">${t('no_playlist')}</div>`;
  document.getElementById('apl-bg').classList.remove('hide');
}
async function addToPl(id){
  const pl=pls[id];pl.tracks=pl.tracks||[];
  pl.tracks.push(pendingTrack);
  await savePl(id,pl);closeApl();toast(t('added_to',pl.name));
}
async function savePl(id,pl){
  await fetch(`/api/playlists/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(pl)});
  pls[id]=pl;
  if(curPl===id){const n=(pl.tracks||[]).length;document.getElementById('pl-cnt').textContent=t('track_count',n);}
}

// ── SEARCH ────────────────────────────────────────────────────
function doSearch(){
  const q=document.getElementById('sinput').value.trim().toLowerCase();
  const el=document.getElementById('sres');
  if(!q){el.innerHTML='';return;}
  const tracks=[],albums=[],artists=[];
  lib.forEach(a=>{
    if(a.name.toLowerCase().includes(q))artists.push(a);
    a.albums.forEach(al=>{
      if(al.name.toLowerCase().includes(q)||(al.artist||'').toLowerCase().includes(q))albums.push(al);
      al.tracks.forEach((t,i)=>{
        const ttl=t.title||(t.filename||'').replace('.webm','');
        if(ttl.toLowerCase().includes(q))tracks.push({...t,title:ttl,albumId:al.id,albumPath:al.path,coverUrl:covUrl(al)||'',artist:al.artist||a.name,albumName:al.name,trackIdx:i});
      });
    });
  });
  let h='';
  if(artists.length){
    h+=`<div class="sres-ttl">Artistes</div>`;
    h+=`<div class="cgrid" style="padding:0;margin-bottom:20px">${artists.slice(0,6).map(a=>{const img=a.albums[0]?covUrl(a.albums[0]):null;return `<div class="card" onclick="showArtist('${x(a.name)}')"><div class="card-cov">${covImg(img)}<div class="card-pbtn"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div></div><div class="card-info"><div class="card-ttl">${x(a.name)}</div></div></div>`;}).join('')}</div>`;
  }
  if(albums.length){
    h+=`<div class="sres-ttl">Albums</div>`;
    h+=`<div class="cgrid" style="padding:0;margin-bottom:20px">${albums.slice(0,6).map(albCard).join('')}</div>`;
  }
  if(tracks.length){
    h+=`<div class="sres-ttl">Titres</div>`;
    h+=tracks.slice(0,20).map((trk,i)=>{
      const regKey=`srch_${i}`;
      trackReg.set(regKey,trk);
      return `<div class="str" onclick="playSearch('${regKey}')">
        <div class="str-cov">${trk.coverUrl?`<img src="${trk.coverUrl}" alt="">`:''}</div>
        <div><div style="font-size:14px;font-weight:600">${x(trk.title)}</div><div style="font-size:12px;color:var(--text2)">${x(trk.artist)} · ${x(trk.albumName)}</div></div>
      </div>`;
    }).join('');
  }
  if(!h)h=`<div style="color:var(--text2);padding:24px 0">${t('no_results',x(q))}</div>`;
  el.innerHTML=h;
}
function playSearch(key){
  const t=typeof key==='string'?trackReg.get(key):key;
  if(!t){console.error('playSearch: track introuvable',key);return;}
  const alb=lib.flatMap(a=>a.albums).find(a=>a.id===t.albumId);
  if(!alb){console.error('playSearch: album introuvable',t.albumId);return;}
  queue=buildQ(alb);qi=t.trackIdx;playT();
}

// ── MODAL UTILS ───────────────────────────────────────────────
function buildSwatches(){
  document.getElementById('swatches').innerHTML=COLORS.map(c=>
    `<div class="swatch${c===selColor?' sel':''}" style="background:${c}" onclick="selCol('${c}')"></div>`
  ).join('');
}
function selCol(c){selColor=c;updSwatches();}
function updSwatches(){document.querySelectorAll('.swatch').forEach(s=>s.classList.toggle('sel',s.style.background===selColor||s.style.backgroundColor===hexRgb(selColor)));}
function hexRgb(h){const r=parseInt(h.slice(1,3),16),g=parseInt(h.slice(3,5),16),b=parseInt(h.slice(5,7),16);return `rgb(${r}, ${g}, ${b})`;}
function closeMod(e){if(e&&e.target!==document.getElementById('modal-bg'))return;document.getElementById('modal-bg').classList.add('hide');}
function closeApl(e){if(e&&e.target!==document.getElementById('apl-bg'))return;document.getElementById('apl-bg').classList.add('hide');pendingTrack=null;}

// ── HELPERS ───────────────────────────────────────────────────
function ft(s){if(!s||isNaN(s))return'0:00';const m=Math.floor(s/60),sec=Math.floor(s%60);return`${m}:${sec.toString().padStart(2,'0')}`;}
function fmtDur(d){if(!d)return'';if(typeof d==='number')return ft(d);if(d.includes(':'))return d;return ft(parseFloat(d));}
function x(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function x2(s){return`'${String(s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'")}'`;}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2800);}

// ── KEYBOARD ──────────────────────────────────────────────────
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.code==='Space'){e.preventDefault();togglePlay();}
  if(e.code==='ArrowRight'&&e.altKey)nextT();
  if(e.code==='ArrowLeft'&&e.altKey)prevT();
  if(e.code==='KeyM')toggleMute();
});
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════════════════════
# FENÊTRE NATIVE PyQt6
# ════════════════════════════════════════════════════════════════════════════
def start_app():
    hide_console()  # au cas où pas encore fait
    from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEngineProfile
    from PyQt6.QtCore import QUrl, QTimer, Qt, QSize, QRect
    from PyQt6.QtGui import (QIcon, QPixmap, QPainter, QColor, QFont,
                             QPen, QPainterPath, QPolygonF)
    import math

    app = QApplication(sys.argv)
    app.setApplicationName("Volta")
    app.setOrganizationName("Volta")

    # ── Fonction utilitaire : dessine le logo bolt en QPainter ───────────────
    def draw_bolt(painter, cx, cy, size, color_accent, color_light):
        """Dessine l'éclair Volta centré en (cx,cy) à l'échelle size."""
        s = size / 112.0
        pts_outer = [
            (cx + 12*s, cy - 56*s),
            (cx - 18*s, cy +  4*s),
            (cx +  6*s, cy +  4*s),
            (cx - 14*s, cy + 58*s),
            (cx + 30*s, cy +  0*s),
            (cx +  8*s, cy +  0*s),
            (cx + 22*s, cy - 56*s),
        ]
        from PyQt6.QtCore import QPointF
        poly = QPolygonF([QPointF(x, y) for x, y in pts_outer])
        painter.setBrush(color_accent)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(poly)
        pen = QPen(color_light); pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(poly)

    def draw_logo_icon(painter, cx, cy, radius):
        """Dessine le logo complet (anneau + éclair) centré en (cx,cy)."""
        rh = QPainter.RenderHint
        painter.setRenderHint(rh.Antialiasing)
        # Cercle fond
        painter.setBrush(QColor("#1c1c26"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(int(cx-radius), int(cy-radius), int(radius*2), int(radius*2))
        # Anneau extérieur
        pen = QPen(QColor("#2a2a3a")); pen.setWidthF(1.5)
        painter.setPen(pen); painter.setBrush(Qt.BrushStyle.NoBrush)
        r2 = radius + 4
        painter.drawEllipse(int(cx-r2), int(cy-r2), int(r2*2), int(r2*2))
        # Arc gauche violet
        from PyQt6.QtCore import QPointF, QRectF
        pen2 = QPen(QColor("#7c6af7")); pen2.setWidthF(max(2, radius*0.045)); pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen2); painter.setBrush(Qt.BrushStyle.NoBrush)
        span = int(radius * 0.85)
        arc_r = QRectF(cx - radius*0.88, cy - radius*0.88, radius*1.76, radius*1.76)
        painter.drawArc(arc_r, 120*16, 120*16)
        # Arc droit lavande
        pen3 = QPen(QColor("#a89eff")); pen3.setWidthF(max(2, radius*0.045)); pen3.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen3)
        painter.drawArc(arc_r, -60*16, 120*16)
        # Éclair
        draw_bolt(painter, cx, cy, radius * 1.0,
                  QColor("#7c6af7"), QColor("#a89eff"))
        # Point central
        painter.setBrush(QColor("#a89eff"))
        painter.setPen(Qt.PenStyle.NoPen)
        dot = max(3, int(radius * 0.06))
        painter.drawEllipse(int(cx-dot), int(cy-dot), dot*2, dot*2)

    # ── Icône fenêtre (64×64) ────────────────────────────────────────────────
    icon_px = QPixmap(64, 64)
    icon_px.fill(Qt.GlobalColor.transparent)
    ip = QPainter(icon_px)
    draw_logo_icon(ip, 32, 32, 26)
    ip.end()

    # ── Splash animé ─────────────────────────────────────────────────────────
    # (imports déjà chargés en tête de start_app)

    W, H = 480, 300

    class SplashWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.WindowStaysOnTopHint |
                Qt.WindowType.SplashScreen
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.resize(W, H)
            # Centre sur l'écran
            screen = QApplication.primaryScreen().geometry()
            self.move((screen.width()-W)//2, (screen.height()-H)//2)

            self._progress = 0       # 0..100 barre de chargement
            self._pulse    = 0.0     # 0..1 pulsation de l'éclair
            self._phase    = 0       # compteur animation
            self._dots     = 0       # points de suspension animés

            # Timer principal 40ms (~25fps)
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(40)

        def _tick(self):
            self._phase += 1
            # Progression simulée qui ralentit vers 90% puis attend Flask
            if self._progress < 88:
                self._progress = min(88, self._progress + 0.9)
            self._pulse = (1 + __import__('math').sin(self._phase * 0.18)) / 2
            if self._phase % 25 == 0:
                self._dots = (self._dots + 1) % 4
            self.update()

        def finish_loading(self):
            """Appeler quand Flask est prêt — complète la barre et ferme."""
            self._progress = 100
            self.update()
            QTimer.singleShot(400, self.close)

        def paintEvent(self, _event):
            import math
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Fond arrondi
            # QPainterPath déjà importé
            path = QPainterPath()
            path.addRoundedRect(0, 0, W, H, 18, 18)
            p.fillPath(path, QColor("#0a0a0f"))
            # Bordure subtile
            pen = QPen(QColor("#2a2a3a")); pen.setWidthF(1.5)
            p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(1, 1, W-2, H-2, 18, 18)

            # Logo icon gauche (animé : légère pulsation)
            pulse = self._pulse
            logo_r = 68 + pulse * 4
            draw_logo_icon(p, 140, 148, logo_r)

            # Texte VOLTA
            fv = QFont("Segoe UI", 38, QFont.Weight.Bold)
            p.setFont(fv)
            p.setPen(QColor("#f0efff"))
            p.drawText(QRect(255, 80, 200, 60), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "VOLTA")

            # Ligne séparatrice
            pen2 = QPen(QColor("#7c6af7")); pen2.setWidthF(1.5)
            p.setPen(pen2)
            p.drawLine(257, 152, 445, 152)

            # Détection langue — une seule fois, en premier
            import locale
            try:
                try:
                    sys_loc = locale.getlocale()[0] or ''
                except Exception:
                    sys_loc = ''
                if not sys_loc:
                    try:
                        sys_loc = locale.getdefaultlocale()[0] or ''
                    except Exception:
                        sys_loc = ''
                code = (sys_loc or 'en').lower().split('_')[0]
            except Exception:
                code = 'en'

            # Sous-titre MUSIC PLAYER (multilingue)
            music_player_lbl = {
                'fr': 'LECTEUR MUSICAL', 'en': 'MUSIC PLAYER',
                'de': 'MUSIKPLAYER',     'es': 'REPRODUCTOR',
                'it': 'LETTORE MUSICALE','pt': 'LEITOR DE MÚSICA',
                'nl': 'MUZIEKSPELER',    'pl': 'ODTWARZACZ',
                'ru': 'МУЗЫКАЛЬНЫЙ ПЛЕЕР',
            }
            fs = QFont("Segoe UI", 10)
            fs.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3.5)
            p.setFont(fs)
            p.setPen(QColor("#9896b8"))
            p.drawText(QRect(259, 158, 200, 28), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       music_player_lbl.get(code, 'MUSIC PLAYER'))

            # Message de chargement avec points animés (multilingue)
            dots = "." * self._dots + " " * (3 - self._dots)
            loading_msgs = {
                'fr': 'Chargement de votre médiathèque',
                'en': 'Loading your library',
                'de': 'Bibliothek wird geladen',
                'es': 'Cargando tu biblioteca',
                'it': 'Caricamento della libreria',
                'pt': 'A carregar a sua biblioteca',
                'nl': 'Bibliotheek laden',
                'pl': 'Ładowanie biblioteki',
                'ru': 'Загрузка библиотеки',
            }
            load_txt = loading_msgs.get(code, loading_msgs['en'])
            msg = f"{load_txt}{dots}"
            fm2 = QFont("Segoe UI", 9)
            p.setFont(fm2)
            p.setPen(QColor("#5c5a78"))
            p.drawText(QRect(0, 224, W, 24), Qt.AlignmentFlag.AlignCenter, msg)

            # Barre de progression
            bar_x, bar_y, bar_w, bar_h = 60, 258, W - 120, 3
            # Fond barre
            p.setBrush(QColor("#1e1e2a")); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(bar_x, bar_y, bar_w, bar_h, 2, 2)
            # Remplissage
            fill_w = int(bar_w * self._progress / 100)
            if fill_w > 0:
                # Dégradé simulé : violet → lavande
                gfill = QColor("#7c6af7")
                if self._progress > 60:
                    t = (self._progress - 60) / 40.0
                    r = int(124 + t * (168 - 124))
                    g = int(106 + t * (158 - 106))
                    b = int(247 + t * (255 - 247))
                    gfill = QColor(r, g, b)
                p.setBrush(gfill)
                p.drawRoundedRect(bar_x, bar_y, fill_w, bar_h, 2, 2)
                # Petit éclat lumineux au bout
                glow_x = bar_x + fill_w - 4
                p.setBrush(QColor(255, 255, 255, 120))
                p.drawEllipse(glow_x, bar_y - 2, 7, 7)

            p.end()

    splash = SplashWindow()
    splash.show()
    app.processEvents()

    # ── démarre le backend Flask dans un thread ───────────────────────────
    t = threading.Thread(target=start_backend, daemon=True)
    t.start()

    # ── fenêtre principale ────────────────────────────────────────────────
    win = QMainWindow()
    win.setWindowTitle("Volta")
    win.resize(1400, 860)
    win.setMinimumSize(QSize(900, 600))
    win.setWindowIcon(QIcon(icon_px))

    # WebEngine
    profile = QWebEngineProfile.defaultProfile()
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)

    view = QWebEngineView()
    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)

    win.setCentralWidget(view)

    def load_app():
        # Charge la page, n'affiche la fenêtre qu'une fois le rendu terminé
        view.setUrl(QUrl(f"http://127.0.0.1:{PORT}"))
        def on_page_loaded(ok):
            splash.finish_loading()
            # Déconnecte pour ne pas retriggerer
            try: view.loadFinished.disconnect(on_page_loaded)
            except: pass
            # Légère pause pour laisser le splash terminer son animation
            QTimer.singleShot(450, win.show)
        view.loadFinished.connect(on_page_loaded)

    # Attendre que Flask soit prêt
    def wait_for_flask(attempt=0):
        import urllib.request
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/library", timeout=1)
            load_app()
        except Exception:
            if attempt < 30:
                QTimer.singleShot(200, lambda: wait_for_flask(attempt + 1))
            else:
                load_app()  # timeout fallback

    QTimer.singleShot(300, wait_for_flask)

    sys.exit(app.exec())


if __name__ == "__main__":
    start_app()
