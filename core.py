"""Persistent wardrobe and image rendering. No network calls in this module."""
import io
import json
import sqlite3
from PIL import Image, ImageChops, ImageOps

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


def _place_product(canvas, item, box):
    raw = item.get('display_photo') or item['photo']
    with Image.open(io.BytesIO(raw)) as source:
        product = _trim_product(source)
    x, y, width, height = box
    product = ImageOps.contain(product, (width, height))
    px = x + (width - product.width) // 2
    py = y + (height - product.height) // 2
    if product.mode == 'RGBA':
        canvas.paste(product, (px, py), product)
    else:
        canvas.paste(product, (px, py))


def collage(items):
    # Clean vertical fashion-board layout: white background, layered tops, bottom and shoes.
    canvas = Image.new('RGB', (1080, 1536), 'white')
    uppers = [i for i in items if i['category'] in ('Верх', 'Верхняя одежда')]
    dresses = [i for i in items if i['category'] == 'Платье / комбинезон']
    bottoms = [i for i in items if i['category'] == 'Низ']
    shoes = [i for i in items if i['category'] == 'Обувь']
    bags = [i for i in items if i['category'] == 'Сумка']
    accessories = [i for i in items if i['category'] == 'Аксессуары']

    if len(uppers) == 1:
        upper_boxes = [(260, 50, 560, 400)]
    elif len(uppers) == 2:
        upper_boxes = [(90, 60, 430, 370), (560, 60, 430, 370)]
    elif len(uppers) == 3:
        upper_boxes = [(35, 70, 320, 340), (380, 45, 320, 365), (725, 70, 320, 340)]
    else:
        upper_boxes = [(25, 70, 245, 320), (285, 45, 245, 345), (545, 45, 245, 345), (805, 70, 245, 320)]
    for item, box in zip(uppers[:4], upper_boxes):
        _place_product(canvas, item, box)

    if dresses:
        _place_product(canvas, dresses[0], (220, 330, 640, 810))
    elif bottoms:
        _place_product(canvas, bottoms[0], (230, 410, 620, 760))

    if shoes:
        _place_product(canvas, shoes[0], (300, 1190, 480, 270))

    side_y = 470
    if bags:
        _place_product(canvas, bags[0], (825, side_y, 210, 210))
        side_y += 230
    for item in accessories[:2]:
        _place_product(canvas, item, (840, side_y, 180, 180))
        side_y += 195

    result = io.BytesIO()
    canvas.save(result, 'JPEG', quality=93)
    return result.getvalue()
