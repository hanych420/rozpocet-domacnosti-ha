from pathlib import Path

path = Path('/app/shopping_list_v104.py')
text = path.read_text(encoding='utf-8')
text = text.replace('VERSION = "0.10.5"', 'VERSION = "0.11.0"', 1)
old = '''            conn.execute(
                "UPDATE items SET preferred_store=?, quantity=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (preferred, quantity, best["id"]),
            )'''
new = '''            conn.execute(
                "UPDATE items SET name=?, name_norm=?, preferred_store=?, quantity=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (deal_name[:160], shopping.normalize_name(deal_name), preferred, quantity, best["id"]),
            )
            shopping._upsert_history(conn, deal_name, 1)'''
if old not in text:
    raise SystemExit('shopping deal merge anchor not found')
text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8')
