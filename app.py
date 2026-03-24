"""
Chocolabs Menu PDF API
Flask API - menu verilerini alıp PyMuPDF ile temiz PDF oluşturur.
Render.com veya benzeri platformda deploy edilir.
"""
import fitz
import os
import sys
import tempfile
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import json

app = Flask(__name__)
CORS(app)  # Shared hosting'den gelen isteklere izin ver

TEMPLATE_PDF = os.path.join(os.path.dirname(__file__), 'menu.pdf')


def extract_fonts(doc):
    """PDF'den SemiBold ve Regular fontları çıkar."""
    page = doc[0]
    fonts = page.get_fonts(full=True)
    semibold_path, regular_path = None, None
    for f in fonts:
        xref, name = f[0], f[3]
        if 'MetronicSlabNarrowSemiBold' in name and 'Ital' not in name:
            font_data = doc.extract_font(xref)
            ext, content = font_data[1], font_data[3]
            if content:
                semibold_path = os.path.join(tempfile.gettempdir(), f"menu_semibold.{ext}")
                with open(semibold_path, "wb") as fp:
                    fp.write(content)
        if 'MetronicSlabNarrowRegular' in name:
            font_data = doc.extract_font(xref)
            ext, content = font_data[1], font_data[3]
            if content:
                regular_path = os.path.join(tempfile.gettempdir(), f"menu_regular.{ext}")
                with open(regular_path, "wb") as fp:
                    fp.write(content)
    return semibold_path, regular_path


def color_to_tuple(c):
    if isinstance(c, (list, tuple)):
        return c
    r = ((c >> 16) & 0xFF) / 255.0
    g = ((c >> 8) & 0xFF) / 255.0
    b = (c & 0xFF) / 255.0
    return (r, g, b)


def find_preserved_labels(page):
    """Küçük stilistik etiketleri bul (size < 3.0)."""
    labels = []
    text_dict = page.get_text("dict")
    for block in text_dict["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if span['size'] < 3.0:
                    labels.append({
                        'text': span['text'],
                        'origin': span['origin'],
                        'font': span['font'],
                        'size': span['size'],
                        'color': color_to_tuple(span['color']),
                        'bbox': span['bbox']
                    })
    return labels


def generate_pdf(items):
    """Menu item verileriyle PDF oluştur."""
    if not os.path.exists(TEMPLATE_PDF):
        raise FileNotFoundError("menu.pdf şablonu bulunamadı")

    doc = fitz.open(TEMPLATE_PDF)
    semibold_path, regular_path = extract_fonts(doc)
    semibold_font = fitz.Font(fontfile=semibold_path) if semibold_path else None

    FALLBACK_ANCHORS = {
        1: {'regular': 176.95, 'mini': 157.94, 'right': 289.50},
        2: {'left': 133.70, 'right': 288.60}
    }

    for page_num in range(doc.page_count):
        page = doc[page_num]
        p_height = page.rect.height
        pnum = page_num + 1

        # Pre-redaction: TL pozisyonlarını yakala
        text_dict = page.get_text("dict")
        all_spans = []
        tl_anchors = []
        for block in text_dict["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    all_spans.append(span)
                    txt = span['text'].strip()
                    if txt == 'TL':
                        tl_anchors.append({'x': span['origin'][0], 'y': span['origin'][1]})
                    elif txt.endswith(' TL'):
                        f_size = span['size']
                        tl_width = semibold_font.text_length("TL", fontsize=f_size) if semibold_font else f_size * 2.0
                        tl_start_x = span['bbox'][2] - tl_width
                        tl_anchors.append({'x': tl_start_x, 'y': span['origin'][1]})

        preserved_labels = find_preserved_labels(page)
        page_items = [i for i in items if i['page'] == pnum]

        # STEP 1: Compute Y shifts for active items
        categories = {}
        for item in page_items:
            cat = item.get('category_id', 0)
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(item)
            
        for cat, cat_items in categories.items():
            original_ys = [i.get('name_y', 0) for i in cat_items]
            active_items = [i for i in cat_items if i.get('is_active', 1) == 1]
            
            for index, a_item in enumerate(active_items):
                orig_name_y = a_item.get('name_y', 0)
                target_name_y = original_ys[index] if index < len(original_ys) else orig_name_y
                a_item['shifted_y'] = target_name_y - orig_name_y

        # STEP 2: Redaction
        for item in page_items:
            is_active = item.get('is_active', 1) == 1
            shift = item.get('shifted_y', 0)
            
            # Inactive items or shifted active items must be completely redacted
            fields_to_redact = ['name', 'desc', 'gram', 'price', 'mini_price'] if (not is_active or shift != 0) else ['price', 'mini_price']
            
            for k in fields_to_redact:
                val = item.get(k)
                if not val: continue
                # target_y is the original Y coordinate
                y_coord = item.get(f"{k}_y", 0)
                if not y_coord: continue
                target_y = p_height - y_coord
                
                val_str = str(val).strip()
                for line in val_str.split('\n'):
                    l = line.strip()
                    if not l: continue
                    rects = page.search_for(l)
                    for r in rects:
                        if r.y0 - 20 <= target_y <= r.y1 + 20:
                            page.add_redact_annot(r, fill=None)
                            
                # Special wipe for 'TL' on prices if it wasn't caught by search_for
                if k in ['price', 'mini_price']:
                    px = item.get(f"{k}_x")
                    if px:
                        for anchor in tl_anchors:
                            if abs(anchor['y'] - target_y) < 3.5 and abs(anchor['x'] - px) < 15:
                                for span in all_spans:
                                    if span['text'].strip() == 'TL' and abs(span['origin'][1] - anchor['y']) < 0.5 and abs(span['origin'][0] - anchor['x']) < 5:
                                        page.add_redact_annot(span['bbox'], fill=None)

        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=False)

        if semibold_path:
            page.insert_font(fontname="price_font", fontfile=semibold_path)
        if regular_path:
            page.insert_font(fontname="regular_font", fontfile=regular_path)

        # STEP 2.5: Preserved labels geri yükle
        for label in preserved_labels:
            try:
                fname = "regular_font" if regular_path else "helv"
                page.insert_text(
                    point=label['origin'], text=label['text'],
                    fontsize=label['size'], fontname=fname, color=label['color']
                )
            except:
                pass

        # STEP 3: Active ürünleri çiz
        def get_best_anchor(orig_x, y, p_no):
            best_ax, min_dist = None, 999
            for anchor in tl_anchors:
                if abs(anchor['y'] - y) < 2.5:
                    dist = anchor['x'] - orig_x
                    if 0 < dist < min_dist:
                        min_dist, best_ax = dist, anchor['x']
            if best_ax:
                return best_ax
            fb = FALLBACK_ANCHORS.get(p_no, {})
            if p_no == 1:
                if orig_x > 220:
                    return fb.get('right', 289.5)
                return fb.get('mini', 157.94) if orig_x < 155 else fb.get('regular', 176.95)
            else:
                return fb.get('right', 288.6) if orig_x > 150 else fb.get('left', 133.7)

        def insert_val(anchor_x, price_str, y, f_size):
            if not price_str:
                return
            num = price_str.replace(' TL', '').strip()
            w = semibold_font.text_length(num, fontsize=f_size) if semibold_font else f_size * 0.5 * len(num)
            num_x = anchor_x - w - 1.6
            fname = "price_font" if semibold_path else "helv"
            try:
                page.insert_text(point=(num_x, y), text=num, fontsize=f_size, fontname=fname, color=(1, 1, 1))
                page.insert_text(point=(anchor_x, y), text="TL", fontsize=f_size, fontname=fname, color=(1, 1, 1))
            except:
                pass

        for item in page_items:
            if item.get('is_active', 1) == 0:
                continue
                
            shift = item.get('shifted_y', 0)
            
            # Eğer shift != 0 ise sildiğimiz ad, içerik ve gramajı yeniden shifted pozisyona çiziyoruz
            if shift != 0:
                name = item.get('name')
                if name:
                    nx = item.get('name_x', 0)
                    ny = p_height - (item.get('name_y', 0) + shift)
                    fname = "price_font" if semibold_path else "helv"
                    fsize = item.get('name_font_size') or 9.5
                    try: page.insert_text((nx, ny), name, fontsize=fsize, fontname=fname, color=(1,1,1))
                    except: pass
                    
                desc = item.get('desc')
                if desc:
                    dx = item.get('desc_x', 0)
                    dy = p_height - (item.get('desc_y', 0) + shift)
                    fname = "regular_font" if regular_path else "helv"
                    fsize = item.get('desc_font_size') or 6.0
                    try: 
                        for i, l in enumerate(desc.split('\n')):
                            page.insert_text((dx, dy + i*(fsize*1.2)), l.strip(), fontsize=fsize, fontname=fname, color=(1,1,1))
                    except: pass
                    
                gram = item.get('gram')
                if gram:
                    gx = item.get('gram_x', 0)
                    gy = p_height - (item.get('gram_y', 0) + shift)
                    fname = "regular_font" if regular_path else "helv"
                    fsize = item.get('gram_font_size') or 5.5
                    try: page.insert_text((gx, gy), gram, fontsize=fsize, fontname=fname, color=(1,1,1))
                    except: pass

            # Fiyatları her zaman çiziyoruz (çünkü her zaman sildik)
            for p_type in ['price', 'mini_price']:
                val = item.get(p_type)
                px = item.get(f'{p_type}_x', 0)
                py = item.get(f'{p_type}_y', 0)
                if not val or px <= 0 or py <= 0:
                    continue
                
                shifted_y = p_height - (py + shift)
                fs = item.get(f'{p_type}_font_size') or 6.4324
                
                original_ty = p_height - py
                ax = get_best_anchor(px, original_ty, pnum)
                if ax:
                    insert_val(ax, val, shifted_y, fs)

    # Geçici dosyaya kaydet - OPTİMİZASYON: garbage=1 kullan (çok daha hızlı)
    output_path = os.path.join(tempfile.gettempdir(), 'output_menu.pdf')
    doc.save(output_path, garbage=1, deflate=False)
    doc.close()

    # Font dosyalarını temizle
    for p in [semibold_path, regular_path]:
        if p and os.path.exists(p):
            os.remove(p)

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
    """
    Menu item verilerini JSON olarak alır, PDF döner.
    
    POST body (JSON):
    {
        "items": [
            {
                "page": 1, "price": "360 TL", "price_x": 167.88, "price_y": 399.86,
                "price_font_size": 5.16, "mini_price": "295 TL", 
                "mini_price_x": 150.42, "mini_price_y": 399.86
            },
            ...
        ]
    }
    """
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
            download_name=f'chocolabs_menu.pdf'
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
