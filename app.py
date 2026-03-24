import fitz
import os
import tempfile
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(__file__)
MENU_PDF = os.path.join(BASE_DIR, 'menu.pdf')

def is_same_text(t1, t2):
    if not t1 or not t2: return False
    return t1.replace('\xa0', ' ').replace(' ', '').strip() == t2.replace('\xa0', ' ').replace(' ', '').strip()

def extract_semibold_font(doc):
    page = doc[0]
    fonts = page.get_fonts(full=True)
    for f in fonts:
        if 'MetronicSlabNarrowSemiBo' in f[3] and 'Ital' not in f[3]:
            ext, content = doc.extract_font(f[0])[1], doc.extract_font(f[0])[3]
            p = os.path.join(tempfile.gettempdir(), f"font_semi.{ext}")
            with open(p, "wb") as fp: fp.write(content)
            return p
    return None

def generate_pdf(items):
    doc = fitz.open(MENU_PDF)
    font_path = extract_semibold_font(doc)

    for page_num in range(doc.page_count):
        page = doc[page_num]
        p_height = page.rect.height
        pnum = page_num + 1

        if font_path: page.insert_font(fontname="f_semi", fontfile=font_path)

        page_items = [i for i in items if i.get('page') == pnum]
        all_spans = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") == 0:
                for l in b["lines"]: all_spans.extend(l["spans"])

        tl_anchors = []
        for s in all_spans:
            txt = s['text'].strip()
            if txt == 'TL': tl_anchors.append({'x': s['origin'][0], 'y': s['origin'][1]})
            elif txt.endswith(' TL'): tl_anchors.append({'x': s['bbox'][2] - s['size']*2.0, 'y': s['origin'][1]})

        draw_commands = []
        for item in page_items:
            for p_type in ['price', 'mini_price']:
                val = str(item.get(p_type) or '').strip()
                px = item.get(f'{p_type}_x', 0)
                py = item.get(f'{p_type}_y', 0)
                if not val or px <= 0 or py <= 0: continue
                
                target_y = p_height - py
                num_new = val.replace(' TL', '').strip()
                fs = item.get(f'{p_type}_font_size') or 6.43

                # Bul veya uydur anchor'ı
                best_ax = px
                best_dist = 999
                for a in tl_anchors:
                    if abs(a['y'] - target_y) < 2.5 and 0 < (a['x'] - px) < best_dist:
                        best_dist, best_ax = a['x'] - px, a['x']

                # Redact
                redacted = False
                for s in all_spans:
                    sy, sx = s['origin'][1], s['origin'][0]
                    # Y aynı hizada ve X de orijinal pozisyona yakın (genelde 30px sapma payı)
                    if abs(sy - target_y) < 2.5 and abs(sx - px) < 30:
                        txt = s['text'].strip()
                        if "TL" in txt and not is_same_text(txt, "TL"):
                            rect = fitz.Rect(s['bbox'])
                            page.add_redact_annot(rect)
                            redacted = True
                        elif txt and txt.replace('.', '').replace(',', '').isdigit():
                            rect = fitz.Rect(s['bbox'])
                            rect.x0 -= 1
                            rect.x1 += 1
                            page.add_redact_annot(rect)
                            redacted = True
                
                if redacted or num_new:
                    draw_commands.append((best_ax, num_new, target_y, fs, 'TL' in val))

        page.apply_redactions()

        for ax, num, target_y, fs, draw_tl in draw_commands:
            fn = "f_semi" if font_path else "helv"
            # Orijinal Metronic Slab width approximation
            w = fs * 0.48 * len(num)
            try:
                page.insert_text((ax - w - 2.0, target_y), num, fontsize=fs, fontname=fn, color=(1,1,1))
                if draw_tl:
                    page.insert_text((ax, target_y), "TL", fontsize=fs, fontname=fn, color=(1,1,1))
            except: pass

    output_path = os.path.join(tempfile.gettempdir(), 'output_menu.pdf')
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    if font_path and os.path.exists(font_path): os.remove(font_path)
    return output_path

@app.route('/', methods=['GET'])
def index(): return jsonify({'status': 'ok'})

@app.route('/generate', methods=['POST'])
def generate():
    try:
        data = request.get_json(force=True)
        items = data.get('items', [])
        if not items: return jsonify({'error': 'items boş'}), 400
        return send_file(generate_pdf(items), mimetype='application/pdf', download_name='chocolabs_menu.pdf')
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
