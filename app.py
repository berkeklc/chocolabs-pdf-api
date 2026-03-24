"""
Chocolabs Menu PDF API
Flask API - menu verilerini alıp PyMuPDF ile temiz PDF oluşturur.
Render.com veya benzeri platformda deploy edilir.

NOT: menu.pdf RUNTIME'DA AÇILMAZ. Tüm fontlar ve statik veriler
preextract.py ile önceden çıkarılmış ve repo'ya commitlenmiştir.
Bu sayede bellek kullanımı ~20MB ile sınırlı kalır.
"""
import fitz
import os
import tempfile
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import json

app = Flask(__name__)
CORS(app)

# Pre-extracted data paths (committed to repo, never changes at runtime)
BASE_DIR = os.path.dirname(__file__)
BLANK_PDF = os.path.join(BASE_DIR, 'menu_blank.pdf')
STATIC_JSON = os.path.join(BASE_DIR, 'static_data.json')
FONT_SEMIBOLD = os.path.join(BASE_DIR, 'font_semibold.cff')
FONT_REGULAR = os.path.join(BASE_DIR, 'font_regular.cff')

# Load static data lazily (on first request, not at startup)
_STATIC_DATA = None

def get_static_data():
    global _STATIC_DATA
    if _STATIC_DATA is None:
        with open(STATIC_JSON, 'r', encoding='utf-8') as f:
            _STATIC_DATA = json.load(f)
    return _STATIC_DATA


def color_to_tuple(c):
    if isinstance(c, (list, tuple)):
        return tuple(c)
    r = ((c >> 16) & 0xFF) / 255.0
    g = ((c >> 8) & 0xFF) / 255.0
    b = (c & 0xFF) / 255.0
    return (r, g, b)


def generate_pdf(items):
    """
    menu_blank.pdf üzerine sıfırdan menü çizer.
    menu.pdf AÇILMAZ - fontlar ve statik veriler dosyadan okunur.
    """
    if not os.path.exists(BLANK_PDF):
        raise FileNotFoundError("menu_blank.pdf bulunamadı")

    doc = fitz.open(BLANK_PDF)
    
    # Load pre-extracted fonts
    semibold_ok = os.path.exists(FONT_SEMIBOLD)
    regular_ok = os.path.exists(FONT_REGULAR)
    semibold_font = fitz.Font(fontfile=FONT_SEMIBOLD) if semibold_ok else None

    for page_num in range(doc.page_count):
        page = doc[page_num]
        pnum = page_num + 1
        pnum_str = str(pnum)
        
        page_static = get_static_data().get(pnum_str)
        if not page_static:
            continue
            
        p_height = page_static['page_height']
        all_spans = page_static['spans']

        # Register fonts on this page
        if semibold_ok:
            page.insert_font(fontname="f_semi", fontfile=FONT_SEMIBOLD)
        if regular_ok:
            page.insert_font(fontname="f_reg", fontfile=FONT_REGULAR)

        # Items for this page
        page_items = [i for i in items if i.get('page') == pnum]
        
        # Convert DB coordinates (from bottom) to PyMuPDF (from top)
        for item in page_items:
            item['_y'] = p_height - item.get('name_y', 0)

        # --- STEP 1: Classify static spans ---
        # Find which spans are "dynamic" (belong to menu items) vs "static" (headers/labels)
        # Also collect TL anchor positions for price alignment
        tl_anchors = []
        static_spans = []
        
        for span in all_spans:
            txt = span['text'].strip()
            if not txt:
                continue
            
            # Collect TL anchors for price alignment
            if txt == 'TL':
                tl_anchors.append({'x': span['x'], 'y': span['y']})
                continue
            if txt.endswith(' TL'):
                # Calculate where the "TL" portion starts
                fs = span['size']
                tl_w = semibold_font.text_length("TL", fontsize=fs) if semibold_font else fs * 2.0
                tl_x = span['bbox'][2] - tl_w
                tl_anchors.append({'x': tl_x, 'y': span['y']})
                continue  # Price values will be drawn from DB data
            
            # Check if this span belongs to any menu item
            is_item_text = False
            sy = span['y']
            for item in page_items:
                iy = item['_y']
                if abs(sy - iy) < 25:
                    n = (item.get('name') or '').strip()
                    g = (item.get('gram') or '').strip()
                    d = str(item.get('desc') or '').strip()
                    if txt == n or txt == g or (d and txt in d):
                        is_item_text = True
                        break
            
            if not is_item_text:
                static_spans.append(span)

        # --- STEP 2: Build column elements ---
        MID_X = 290  # Left/right column boundary
        col1 = []  # Left column
        col2 = []  # Right column
        
        for s in static_spans:
            entry = {'type': 'static', 'y': s['y'], 'x': s['x'], 'data': s}
            if s['x'] < MID_X:
                col1.append(entry)
            else:
                col2.append(entry)
        
        for item in page_items:
            x = item.get('name_x', 0)
            entry = {
                'type': 'item', 'y': item['_y'], 'x': x,
                'active': item.get('is_active', 1) == 1,
                'cat': item.get('category_id', 0),
                'data': item
            }
            if x < MID_X:
                col1.append(entry)
            else:
                col2.append(entry)

        # --- STEP 3: Process shifts per column ---
        def process_column(col):
            col.sort(key=lambda e: e['y'])
            
            # Calculate typical row spacing per category
            spacings = {}
            for i in range(len(col) - 1):
                a, b = col[i], col[i + 1]
                if a['type'] == 'item' and b['type'] == 'item' and a['cat'] == b['cat']:
                    gap = b['y'] - a['y']
                    if 10 < gap < 40:
                        spacings[a['cat']] = gap
            
            shift = 0
            result = []
            for e in col:
                if e['type'] == 'item' and not e['active']:
                    shift += spacings.get(e['cat'], 20.0)
                else:
                    e['new_y'] = e['y'] - shift
                    result.append(e)
            return result

        drawn = process_column(col1) + process_column(col2)

        # --- STEP 4: Price anchor helper ---
        FALLBACK_ANCHORS = {
            1: {'regular': 176.95, 'mini': 157.94, 'right': 289.50},
            2: {'left': 133.70, 'right': 288.60}
        }
        
        def get_anchor(orig_x, orig_y):
            best, best_dist = None, 999
            for a in tl_anchors:
                if abs(a['y'] - orig_y) < 2.5:
                    d = a['x'] - orig_x
                    if 0 < d < best_dist:
                        best_dist, best = d, a['x']
            if best:
                return best
            fb = FALLBACK_ANCHORS.get(pnum, {})
            if pnum == 1:
                if orig_x > 220: return fb.get('right', 289.5)
                return fb.get('mini', 157.94) if orig_x < 155 else fb.get('regular', 176.95)
            return fb.get('right', 288.6) if orig_x > 150 else fb.get('left', 133.7)

        def draw_price(anchor_x, price_str, y, fs):
            if not price_str:
                return
            num = str(price_str).replace(' TL', '').strip()
            if not num:
                return
            w = semibold_font.text_length(num, fontsize=fs) if semibold_font else fs * 0.5 * len(num)
            num_x = anchor_x - w - 1.6
            fn = "f_semi" if semibold_ok else "helv"
            try:
                page.insert_text((num_x, y), num, fontsize=fs, fontname=fn, color=(1, 1, 1))
                page.insert_text((anchor_x, y), "TL", fontsize=fs, fontname=fn, color=(1, 1, 1))
            except:
                pass

        # --- STEP 5: Draw everything ---
        for e in drawn:
            ny = e['new_y']
            
            if e['type'] == 'static':
                s = e['data']
                fn = "f_semi" if semibold_ok else "helv"
                if 'Regular' in s.get('font', '') or 'Regula' in s.get('font', ''):
                    fn = "f_reg" if regular_ok else "helv"
                color = color_to_tuple(s.get('color', [1, 1, 1]))
                try:
                    page.insert_text((s['x'], ny), s['text'], fontsize=s['size'], fontname=fn, color=color)
                except:
                    pass
            
            elif e['type'] == 'item':
                item = e['data']
                dy = item['_y'] - ny  # How much this item shifted up
                
                # Name
                name = item.get('name')
                if name:
                    fn = "f_semi" if semibold_ok else "helv"
                    fs = item.get('name_font_size') or 9.5
                    try:
                        page.insert_text(
                            (item.get('name_x', 0), p_height - item.get('name_y', 0) - dy),
                            str(name), fontsize=fs, fontname=fn, color=(1, 1, 1))
                    except:
                        pass
                
                # Description
                desc = item.get('desc')
                if desc:
                    fn = "f_reg" if regular_ok else "helv"
                    fs = item.get('desc_font_size') or 6.0
                    dx = item.get('desc_x', 0)
                    desc_y = p_height - item.get('desc_y', 0) - dy
                    try:
                        for li, line in enumerate(str(desc).split('\n')):
                            page.insert_text((dx, desc_y + li * (fs * 1.3)), line.strip(), fontsize=fs, fontname=fn, color=(1, 1, 1))
                    except:
                        pass
                
                # Gram
                gram = item.get('gram')
                if gram:
                    fn = "f_reg" if regular_ok else "helv"
                    fs = item.get('gram_font_size') or 5.5
                    try:
                        page.insert_text(
                            (item.get('gram_x', 0), p_height - item.get('gram_y', 0) - dy),
                            str(gram), fontsize=fs, fontname=fn, color=(1, 1, 1))
                    except:
                        pass
                
                # Prices
                for pt in ['price', 'mini_price']:
                    val = item.get(pt)
                    px = item.get(f'{pt}_x', 0)
                    py_val = item.get(f'{pt}_y', 0)
                    if not val or px <= 0 or py_val <= 0:
                        continue
                    fs = item.get(f'{pt}_font_size') or 6.43
                    orig_y = p_height - py_val
                    ax = get_anchor(px, orig_y)
                    if ax:
                        draw_price(ax, str(val), p_height - py_val - dy, fs)

    output_path = os.path.join(tempfile.gettempdir(), 'output_menu.pdf')
    doc.save(output_path, garbage=1, deflate=False)
    doc.close()
    return output_path


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'service': 'Chocolabs Menu PDF API',
        'status': 'ok',
        'usage': 'POST /generate ile menu verilerini gönderin'
    })


@app.route('/generate', methods=['POST'])
def generate():
    try:
        data = request.get_json(force=True)
        if not data or 'items' not in data:
            return jsonify({'error': 'items alanı gerekli'}), 400

        items = data['items']
        if not isinstance(items, list) or len(items) == 0:
            return jsonify({'error': 'items boş olamaz'}), 400

        output_path = generate_pdf(items)

        return send_file(
            output_path,
            mimetype='application/pdf',
            as_attachment=False,
            download_name='chocolabs_menu.pdf'
        )

    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
