"""Persistent wardrobe and image rendering. No network calls in this module."""
import io
import json
import sqlite3
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
            source TEXT NOT NULL, category TEXT NOT NULL, description TEXT NOT NULL,
            photo BLOB NOT NULL, UNIQUE(uid, source));
          CREATE TABLE IF NOT EXISTS state (uid INTEGER PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outfits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.commit()

    def consent(self, uid):
        return bool(self.db.execute('SELECT 1 FROM users WHERE uid=? AND consent=1', (uid,)).fetchone())

    def accept(self, uid):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO users VALUES (?,1)', (uid,))

    def items(self, uid):
        return [dict(r) for r in self.db.execute('SELECT id,category,description FROM items WHERE uid=? ORDER BY id', (uid,))]

    def item(self, uid, iid):
        r = self.db.execute('SELECT * FROM items WHERE uid=? AND id=?', (uid, iid)).fetchone()
        if not r:
            raise UserError('Вещь уже удалена или недоступна.')
        return dict(r)

    def duplicate(self, uid, source):
        return self.db.execute('SELECT id FROM items WHERE uid=? AND source=?', (uid, source)).fetchone()

    def add(self, uid, source, category, description, photo):
        if category not in CATEGORIES:
            raise UserError('Не удалось определить категорию. Попробуй другое фото.')
        with self.db:
            return self.db.execute('INSERT INTO items(uid,source,category,description,photo) VALUES (?,?,?,?,?)',
                                   (uid, source, category, description[:600], photo)).lastrowid

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
        raise UserError(result.get('message', 'Недостаточно вещей для трёх образов.')[:800])
    if len(outfits) != 3:
        raise UserError('Не удалось составить три разных образа. Добавь вещей или измени параметры.')
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
            raise UserError('В гардеробе не получилось найти три разных комплекта. Добавь вещей.')
        seen.add(key)
    return outfits

def collage(items):
    width, cell, gap = 1000, 460, 24
    rows = (len(items) + 1) // 2
    canvas = Image.new('RGB', (width, 88 + rows * 520), '#f4f1ec')
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 23)
        title = ImageFont.truetype('DejaVuSans.ttf', 34)
    except OSError:
        font = title = ImageFont.load_default()
    draw.text((28, 22), 'ТВОЙ ОБРАЗ', font=title, fill='#28372e')
    for n, item in enumerate(items):
        x, y = 28 + (n % 2) * (cell + gap), 88 + (n // 2) * 520
        draw.rounded_rectangle((x, y, x + cell, y + 448), radius=18, fill='white')
        with Image.open(io.BytesIO(item['photo'])) as source:
            im = ImageOps.contain(source.convert('RGB'), (cell - 24, 422))
        canvas.paste(im, (x + (cell-im.width)//2, y + (448-im.height)//2))
        draw.text((x+4, y+461), f"#{item['id']} · {item['category']}", font=font, fill='#28372e')
    result = io.BytesIO()
    canvas.save(result, 'JPEG', quality=87)
    return result.getvalue()
