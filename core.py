"""Persistent wardrobe and image rendering. No network calls in this module."""
import io
import json
import sqlite3
from PIL import Image, ImageChops, ImageDraw, ImageOps

CATEGORIES = ['Верх', 'Низ', 'Платье / комбинезон', 'Верхняя одежда', 'Обувь', 'Сумка', 'Аксессуары']
QUESTIONS = [
    ('temperature', 'Какая температура?', ['Ниже 0 °C', '0–10 °C', '10–20 °C', '20–25 °C', 'Выше 25 °C']),
    ('weather', 'Какая погода?', ['Сухо', 'Дождь', 'Снег', 'Ветрено']),
    ('occasion', 'Куда собираешься?', ['На каждый день', 'На работу', 'На прогулку', 'На свидание', 'На праздник']),
    ('style', 'Какой стиль?', ['На усмотрение стилиста', 'Повседневный', 'Деловой', 'Спортивный', 'Минимализм']),
    ('colors', 'Какие цвета предпочитаешь?', ['Любые', 'Нейтральные', 'Яркие акценты', 'Монохром']),
]

class UserError(Exception):
    pass

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, consent INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
            source TEXT NOT NULL, category TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL, photo BLOB NOT NULL, display_photo BLOB,
            UNIQUE(uid, source));
          CREATE TABLE IF NOT EXISTS state (uid INTEGER PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outfits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(items)')}
        if 'item_type' not in columns:
            self.db.execute("ALTER TABLE items ADD COLUMN item_type TEXT NOT NULL DEFAULT ''")
        if 'display_photo' not in columns:
            self.db.execute('ALTER TABLE items ADD COLUMN display_photo BLOB')
        self.db.execute("UPDATE items SET item_type=category WHERE item_type='' OR item_type IS NULL")
        self.db.execute('UPDATE items SET display_photo=photo WHERE display_photo IS NULL')
        self.db.commit()

    def consent(self, uid):
        return bool(self.db.execute('SELECT 1 FROM users WHERE uid=? AND consent=1', (uid,)).fetchone())

    def accept(self, uid):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO users VALUES (?,1)', (uid,))

    def items(self, uid):
        return [dict(r) for r in self.db.execute(
            'SELECT id,category,item_type,description FROM items WHERE uid=? ORDER BY id', (uid,))]

    def item(self, uid, iid):
        r = self.db.execute('SELECT * FROM items WHERE uid=? AND id=?', (uid, iid)).fetchone()
        if not r:
            raise UserError('Вещь уже удалена или недоступна.')
        return dict(r)

    def duplicate(self, uid, source):
        return self.db.execute('SELECT id FROM items WHERE uid=? AND source=?', (uid, source)).fetchone()

    def add(self, uid, source, category, description, photo, item_type='', display_photo=None):
        if category not in CATEGORIES:
            raise UserError('Не удалось определить категорию. Попробуй другое фото.')
        item_type = (item_type or category)[:80]
        with self.db:
            return self.db.execute(
                'INSERT INTO items(uid,source,category,item_type,description,photo,display_photo) VALUES (?,?,?,?,?,?,?)',
                (uid, source, category, item_type, description[:600], photo, display_photo or photo)
            ).lastrowid

    def change_category(self, uid, iid, category):
        self.item(uid, iid)
        with self.db:
            self.db.execute('UPDATE items SET category=? WHERE uid=? AND id=?', (category, uid, iid))
            self.db.execute('DELETE FROM outfits WHERE uid=?', (uid,))

    def delete(self, uid, iid):
        self.item(uid, iid)
        with self.db:
            self.db.execute('DELETE FROM items WHERE uid=? AND id=?', (uid, iid))
            self.db.execute('DELETE FROM outfits WHERE uid=?', (uid,))

    def erase(self, uid):
        with self.db:
            for table in ('items', 'outfits', 'state', 'users'):
                self.db.execute(f'DELETE FROM {table} WHERE uid=?', (uid,))
        self.db.execute('VACUUM')

    def state(self, uid, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (uid, json.dumps(value)))
            return value
        r = self.db.execute('SELECT data FROM state WHERE uid=?', (uid,)).fetchone()
        return json.loads(r[0]) if r else {}

    def save_outfit(self, uid, data):
        with self.db:
            oid = self.db.execute('INSERT INTO outfits(uid,data) VALUES (?,?)', (uid, json.dumps(data))).lastrowid
            self.db.execute('DELETE FROM outfits WHERE uid=? AND id NOT IN (SELECT id FROM outfits WHERE uid=? ORDER BY id DESC LIMIT 30)', (uid, uid))
            return oid

    def outfit(self, uid, oid):
        r = self.db.execute('SELECT data FROM outfits WHERE uid=? AND id=?', (uid, oid)).fetchone()
        if not r:
            raise UserError('Этот образ устарел. Нажми «Подобрать образы».')
        data = json.loads(r[0])
        for iid in data['ids']:
            self.item(uid, iid)
        return data

    def replace(self, uid, oid, old, new):
        data = self.outfit(uid, oid)
        before, after = self.item(uid, old), self.item(uid, new)
        if old not in data['ids'] or new in data['ids'] or before['category'] != after['category']:
            raise UserError('Замена недоступна. Выбери другую вещь.')
        data['ids'] = [new if i == old else i for i in data['ids']]
        data['reason'] = 'Комплект с твоей заменой.'
        with self.db:
            self.db.execute('UPDATE outfits SET data=? WHERE uid=? AND id=?', (json.dumps(data), uid, oid))

    def offset(self, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset',?)", (str(value),))
        r = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(r[0]) if r else 0

def normalize_photo(raw):
    if len(raw) > 12 * 1024 * 1024:
        raise UserError('Фото слишком большое. Отправь его как фото, а не файл.')
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 25_000_000:
            raise UserError('Уменьши разрешение фотографии.')
        im = ImageOps.exif_transpose(source).convert('RGB')
        im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=90)
        return buf.getvalue()

def validate_outfits(result, inventory):
    outfits = result.get('outfits', [])
    if not outfits:
        raise UserError(result.get('message', 'Недостаточно вещей для подходящего образа.')[:800])
    if not 1 <= len(outfits) <= 3:
        raise UserError('ИИ вернул некорректное количество образов. Попробуй ещё раз.')
    known = {i['id']: i for i in inventory}
    seen = set()
    for outfit in outfits:
        ids = outfit['ids']
        if not 2 <= len(ids) <= 8 or len(ids) != len(set(ids)) or any(i not in known for i in ids):
            raise UserError('ИИ предложил некорректный комплект. Попробуй ещё раз.')
        categories = {known[i]['category'] for i in ids}
        if not ('Обувь' in categories and ('Платье / комбинезон' in categories or {'Верх', 'Низ'} <= categories)):
            raise UserError('Для полного образа нужны обувь и верх с низом либо платье / комбинезон.')
        key = tuple(sorted(ids))
        if key in seen:
            raise UserError('Образы должны отличаться набором вещей. Попробуй ещё раз.')
        seen.add(key)
    return outfits

def _trim_product(im):
    im = im.convert('RGBA')
    alpha = im.getchannel('A')
    alpha_box = alpha.getbbox()
    if alpha_box and alpha.getextrema()[0] < 255:
        return im.crop(alpha_box)

    rgb = im.convert('RGB')
    diff = ImageChops.difference(rgb, Image.new('RGB', rgb.size, 'white')).convert('L')
    mask = diff.point(lambda p: 255 if p > 18 else 0)
    box = mask.getbbox()
    return im.crop(box) if box else im


def _item_text(item):
    return f"{item.get('item_type') or ''} {item.get('description') or ''}".lower()


def _bottom_kind(item):
    text = _item_text(item)
    if any(word in text for word in ('шорт', 'бермуд', 'мини-юб', 'мини юб')):
        return 'short'
    if any(word in text for word in ('юбк', 'skirt')):
        return 'skirt'
    if any(word in text for word in ('брюк', 'джинс', 'штаны', 'леггин', 'карго', 'клеш', 'клёш')):
        return 'long'
    return 'regular'


def _upper_layout(count):
    """Equal visual cells for upper layers. Two layers are exactly a 50/50 split."""
    count = max(1, min(4, count))
    left, top, total_width, height = 70, 55, 940, 340
    gap = 22
    cell_width = (total_width - gap * (count - 1)) // count
    return [
        (left + n * (cell_width + gap), top, cell_width, height)
        for n in range(count)
    ]


def _bottom_layout(item):
    """Category-aware box keeps shorts from looking larger than tops and shoes."""
    kind = _bottom_kind(item)
    if kind == 'short':
        return (245, 485, 590, 315), 900
    if kind == 'skirt':
        return (260, 465, 560, 410), 975
    if kind == 'long':
        return (270, 420, 540, 650), 1160
    return (260, 445, 560, 520), 1080


def _place_product(canvas, item, box, fill=0.90, valign='center'):
    raw = item.get('display_photo') or item['photo']
    with Image.open(io.BytesIO(raw)) as source:
        product = _trim_product(source)

    x, y, width, height = box
    target_width = max(1, int(width * fill))
    target_height = max(1, int(height * fill))
    product = ImageOps.contain(product, (target_width, target_height))

    px = x + (width - product.width) // 2
    if valign == 'top':
        py = y
    elif valign == 'bottom':
        py = y + height - product.height
    else:
        py = y + (height - product.height) // 2

    if product.mode == 'RGBA':
        canvas.paste(product, (px, py), product)
    else:
        canvas.paste(product, (px, py))


def collage(items):
    """Vertical fashion board with consistent visual scale and split upper layers."""
    canvas = Image.new('RGB', (1080, 1536), 'white')
    draw = ImageDraw.Draw(canvas)

    tops = [i for i in items if i['category'] == 'Верх']
    outerwear = [i for i in items if i['category'] == 'Верхняя одежда']
    uppers = (tops + outerwear)[:4]
    dresses = [i for i in items if i['category'] == 'Платье / комбинезон']
    bottoms = [i for i in items if i['category'] == 'Низ']
    shoes = [i for i in items if i['category'] == 'Обувь']
    bags = [i for i in items if i['category'] == 'Сумка']
    accessories = [i for i in items if i['category'] == 'Аксессуары']

    # Upper area. When there are two layers, each gets exactly half of the visual zone.
    if uppers:
        upper_boxes = _upper_layout(len(uppers))
        for n, (item, box) in enumerate(zip(uppers, upper_boxes)):
            _place_product(canvas, item, box, fill=0.88, valign='bottom')
            if n < len(upper_boxes) - 1:
                divider_x = box[0] + box[2] + 11
                draw.line((divider_x, 105, divider_x, 355), fill=(232, 232, 232), width=2)

    # Main garment area and shoe baseline adapt to the type of bottom.
    if dresses:
        _place_product(canvas, dresses[0], (250, 410, 580, 690), fill=0.90, valign='top')
        shoe_y = 1160
    elif bottoms:
        bottom_box, shoe_y = _bottom_layout(bottoms[0])
        _place_product(canvas, bottoms[0], bottom_box, fill=0.88, valign='top')
    else:
        shoe_y = 1050

    if shoes:
        _place_product(canvas, shoes[0], (310, shoe_y, 460, 190), fill=0.90, valign='center')

    # Small items stay secondary and never determine the scale of the main outfit.
    side_y = 500
    if bags:
        _place_product(canvas, bags[0], (850, side_y, 175, 175), fill=0.82)
        side_y += 195
    for item in accessories[:2]:
        _place_product(canvas, item, (865, side_y, 145, 145), fill=0.78)
        side_y += 165

    result = io.BytesIO()
    canvas.save(result, 'JPEG', quality=94)
    return result.getvalue()
