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


def is_same_text(s1, s2):
    return str(s1).strip() == str(s2).strip()

def generate_pdf(items):
    """Menu_blank.pdf uzerine sifirdan yepyeni, mukemmel hizali menu cizer."""
    BLANK_PDF = os.path.join(os.path.dirname(__file__), 'menu_blank.pdf')
    ORIGINAL_PDF = os.path.join(os.path.dirname(__file__), 'menu.pdf')
    if not os.path.exists(BLANK_PDF):
        raise FileNotFoundError("menu_blank.pdf şablonu bulunamadı")
        
    doc_orig = fitz.open(ORIGINAL_PDF)
    doc_blank = fitz.open(BLANK_PDF)
    
    semibold_path, regular_path = extract_fonts(doc_orig)
    semibold_font = fitz.Font(fontfile=semibold_path) if semibold_path else None
    
    FALLBACK_ANCHORS = {
        1: {'regular': 176.95, 'mini': 157.94, 'right': 289.50},
        2: {'left': 133.70, 'right': 288.60}
    }

    # Iterate over tools
    for page_num in range(doc_blank.page_count):
        page_orig = doc_orig[page_num] if page_num < doc_orig.page_count else None
        page_blank = doc_blank[page_num]
        p_height = page_blank.rect.height
        pnum = page_num + 1
        
        # Original spandata
        text_dict = page_orig.get_text("dict") if page_orig else {"blocks": []}
        all_spans = []
        tl_anchors = []
        for block in text_dict["blocks"]:
            if block["type"] != 0: continue
            for line in block["lines"]:
                for span in line["spans"]:
                    all_spans.append(span)
                    txt = span['text'].strip()
                    if txt == 'TL':
                        tl_anchors.append({'x': span['origin'][0], 'y': span['origin'][1]})
                    elif txt.endswith(' TL'):
                        fs = span['size']
                        tl_width = semibold_font.text_length("TL", fontsize=fs) if semibold_font else fs * 2.0
                        tl_start_x = span['bbox'][2] - tl_width
                        tl_anchors.append({'x': tl_start_x, 'y': span['origin'][1]})

        page_items = [i for i in items if i.get('page') == pnum]
        
        # We need to map page_items to PyMuPDF 'origin' Y coordinates (from top)
        for i in page_items:
            # DB defined coordinates are from the bottom. Convert to top-down for PyMuPDF
            i['pymupdf_y'] = p_height - i.get('name_y', 0)
            
        def get_best_anchor(orig_x, y, p_no):
            best_ax, min_dist = None, 999
            for anchor in tl_anchors:
                if abs(anchor['y'] - y) < 2.5:
                    dist = anchor['x'] - orig_x
                    if 0 < dist < min_dist:
                        min_dist, best_ax = dist, anchor['x']
            if best_ax: return best_ax
            fb = FALLBACK_ANCHORS.get(p_no, {})
            if p_no == 1:
                if orig_x > 220: return fb.get('right', 289.5)
                return fb.get('mini', 157.94) if orig_x < 155 else fb.get('regular', 176.95)
            else:
                return fb.get('right', 288.6) if orig_x > 150 else fb.get('left', 133.7)

        # 1. Identify "Static Spans" (Headers, labels) from original PDF
        # We find spans that DO NOT match any item's strings (name, desc, gram, price)
        static_spans = []
        for span in all_spans:
            txt = span['text'].strip()
            if not txt: continue
            
            # The 'TL' signs next to prices will be handled by insert_val
            # But the 'TL' in '360 TL' could be part of the price string.
            if txt == 'TL':
                continue
                
            y_topdown = span['origin'][1]
            x_left = span['origin'][0]
            
            is_dynamic_item = False
            for item in page_items:
                # We do rough Y matching
                item_y = item['pymupdf_y']
                if abs(y_topdown - item_y) < 25:
                    # check if string matches
                    if is_same_text(txt, item.get('name')) or \
                       is_same_text(txt, item.get('gram')) or \
                       is_same_text(txt, (item.get('price') or '').replace(' TL','')) or \
                       is_same_text(txt, (item.get('mini_price') or '').replace(' TL','')):
                        is_dynamic_item = True
                        break
                    # Desc is multiline, check substring
                    if item.get('desc') and txt in str(item.get('desc')):
                        is_dynamic_item = True
                        break
            
            if not is_dynamic_item:
                static_spans.append(span)
                
        # 2. Separate EVERYTHING (Active Items, Inactive Items, Static Spans) into 2 Columns
        # Column 1: x < 300, Column 2: x >= 300
        # Exception: Wide spans at the top "FİYATLAR ... DAHİLDİR" (x<300 but very wide).
        col1 = []
        col2 = []
        
        # Add static spans to columns
        for s in static_spans:
            x, y = s['origin'][0], s['origin'][1]
            # Page center is approx 297. Right column elements usually start around 300.
            # E.g. "ÇİKOLATA KUTULARI" is right column.
            # Page width is ~595. Midpoint is 297.
            if x < 290:
                col1.append({'type': 'static', 'y': y, 'x': x, 'data': s})
            else:
                col2.append({'type': 'static', 'y': y, 'x': x, 'data': s})
                
        # Add dynamic items to columns
        for item in page_items:
            x = item.get('name_x', 0)
            y = item['pymupdf_y']
            is_act = item.get('is_active', 1) == 1
            cat = item.get('category_id', 0)
            if x < 290:
                col1.append({'type': 'item', 'y': y, 'x': x, 'is_act': is_act, 'data': item, 'cat': cat})
            else:
                col2.append({'type': 'item', 'y': y, 'x': x, 'is_act': is_act, 'data': item, 'cat': cat})

        # 3. Process cumulative shifts for each column
        def process_column(col_elements):
            col_elements.sort(key=lambda e: e['y']) # Sort Top to Bottom (smallest Y first)
            
            # Estimate standard item spacing for categories. Usually 20 points.
            cat_spacing = {}
            for i in range(len(col_elements)-1):
                e1 = col_elements[i]
                e2 = col_elements[i+1]
                if e1['type'] == 'item' and e2['type'] == 'item' and e1['cat'] == e2['cat']:
                    gap = e2['y'] - e1['y']
                    if 10 < gap < 40:
                        cat_spacing[e1['cat']] = gap
                        
            current_shift = 0 # How much to slide everything UPWARDS (subtract from PyMuPDF Y)
            drawn_elements = []
            
            for e in col_elements:
                if e['type'] == 'item' and not e['is_act']:
                    # Item is deactivated! The space it occupied must be collapsed.
                    # Increase the upward shift for all elements BELOW it.
                    space = cat_spacing.get(e['cat'], 20.0)
                    current_shift += space
                else:
                    target_y = e['y'] - current_shift
                    e['shifted_y'] = target_y
                    drawn_elements.append(e)
            return drawn_elements

        drawn_col1 = process_column(col1)
        drawn_col2 = process_column(col2)
        
        all_drawn = drawn_col1 + drawn_col2
        
        if semibold_path: page_blank.insert_font(fontname="price_font", fontfile=semibold_path)
        if regular_path: page_blank.insert_font(fontname="regular_font", fontfile=regular_path)

        def insert_val(page_dst, anchor_x, price_str, ty, f_size):
            if not price_str: return
            num = price_str.replace(' TL', '').strip()
            w = semibold_font.text_length(num, fontsize=f_size) if semibold_font else f_size * 0.5 * len(num)
            num_x = anchor_x - w - 1.6
            fname = "price_font" if semibold_path else "helv"
            try:
                page_dst.insert_text(point=(num_x, ty), text=num, fontsize=f_size, fontname=fname, color=(1, 1, 1))
                page_dst.insert_text(point=(anchor_x, ty), text="TL", fontsize=f_size, fontname=fname, color=(1, 1, 1))
            except: pass

        # 4. Draw EVERYTHING onto menu_blank.pdf
        for e in all_drawn:
            target_y = e['shifted_y']
            
            if e['type'] == 'static':
                s = e['data']
                # The text baseline originally was s['origin'][1]. We changed it to target_y.
                new_origin = (s['origin'][0], target_y)
                # s['font'] contains the original font name. We don't have the original font object mounted.
                # However, most static texts use regular_font or price_font
                fname = "price_font" if semibold_path else "helv"
                if 'Regular' in s['font']:
                    fname = "regular_font" if regular_path else "helv"
                    
                color = color_to_tuple(s.get('color', (1,1,1)))
                try: page_blank.insert_text(point=new_origin, text=s['text'], fontsize=s['size'], fontname=fname, color=color)
                except: pass
                
            elif e['type'] == 'item':
                item = e['data']
                shift = item['pymupdf_y'] - target_y # (How much it went up)
                
                # Draw Name
                name = item.get('name')
                if name:
                    nx = item.get('name_x', 0)
                    ny = p_height - item.get('name_y', 0) - shift
                    fname = "price_font" if semibold_path else "helv"
                    fsize = item.get('name_font_size') or 9.5
                    try: page_blank.insert_text((nx, ny), name, fontsize=fsize, fontname=fname, color=(1,1,1))
                    except: pass
                    
                # Draw Desc
                desc = item.get('desc')
                if desc:
                    dx = item.get('desc_x', 0)
                    dy = p_height - item.get('desc_y', 0) - shift
                    fname = "regular_font" if regular_path else "helv"
                    fsize = item.get('desc_font_size') or 6.0
                    try: 
                        for i, l in enumerate(str(desc).split('\n')):
                            page_blank.insert_text((dx, dy + i*(fsize*1.3)), l.strip(), fontsize=fsize, fontname=fname, color=(1,1,1))
                    except: pass
                    
                # Draw Gram
                gram = item.get('gram')
                if gram:
                    gx = item.get('gram_x', 0)
                    gy = p_height - item.get('gram_y', 0) - shift
                    fname = "regular_font" if regular_path else "helv"
                    fsize = item.get('gram_font_size') or 5.5
                    try: page_blank.insert_text((gx, gy), str(gram), fontsize=fsize, fontname=fname, color=(1,1,1))
                    except: pass

                # Prices
                for p_type in ['price', 'mini_price']:
                    val = item.get(p_type)
                    px = item.get(f'{p_type}_x', 0)
                    py = item.get(f'{p_type}_y', 0)
                    if not val or px <= 0 or py <= 0: continue
                    
                    shifted_py = p_height - py - shift
                    fs = item.get(f'{p_type}_font_size') or 6.4324
                    
                    original_ty = p_height - py
                    ax = get_best_anchor(px, original_ty, pnum)
                    if ax:
                        insert_val(page_blank, ax, str(val), shifted_py, fs)

    # Optimizasyon gerekmiyor cunku redaction yok ve kucuk dosyalar hizli kaydedilir
    output_path = os.path.join(tempfile.gettempdir(), 'output_menu.pdf')
    doc_blank.save(output_path, garbage=3, deflate=True)
    doc_orig.close()
    doc_blank.close()

    for p in [semibold_path, regular_path]:
        if p and os.path.exists(p): os.remove(p)

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
