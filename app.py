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

        # STEP 1: Redaction
        for item in page_items:
            for p_type in ['price', 'mini_price']:
                val = item.get(p_type)
                px = item.get(f'{p_type}_x', 0)
                py = item.get(f'{p_type}_y', 0)
                if not val or px <= 0 or py <= 0:
                    continue
                target_y = p_height - py
                for span in all_spans:
                    if abs(span['origin'][1] - target_y) < 2.5 and abs(span['origin'][0] - px) < 5:
                        page.add_redact_annot(span['bbox'], fill=None)

        for anchor in tl_anchors:
            for span in all_spans:
                if abs(span['origin'][1] - anchor['y']) < 0.5:
                    if span['text'].strip() == 'TL' and abs(span['origin'][0] - anchor['x']) < 5:
                        page.add_redact_annot(span['bbox'], fill=None)

        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=False)

        if semibold_path:
            page.insert_font(fontname="price_font", fontfile=semibold_path)
        if regular_path:
            page.insert_font(fontname="regular_font", fontfile=regular_path)

        # STEP 2: Preserved labels geri yükle
        for label in preserved_labels:
            try:
                fname = "regular_font" if regular_path else "helv"
                page.insert_text(
                    point=label['origin'], text=label['text'],
                    fontsize=label['size'], fontname=fname, color=label['color']
                )
            except:
                pass

        # STEP 3: Yeni fiyatları yaz
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
            for p_type in ['price', 'mini_price']:
                val = item.get(p_type)
                px = item.get(f'{p_type}_x', 0)
                py = item.get(f'{p_type}_y', 0)
                if not val or px <= 0 or py <= 0:
                    continue
                ty = p_height - py
                fs = item.get('price_font_size') or 6.4324
                ax = get_best_anchor(px, ty, pnum)
                insert_val(ax, val, ty, fs)

    # Geçici dosyaya kaydet
    output_path = os.path.join(tempfile.gettempdir(), 'output_menu.pdf')
    doc.save(output_path, garbage=4, deflate=True)
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
