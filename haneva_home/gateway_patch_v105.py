from pathlib import Path

path = Path('/app/consolidated_gateway.py')
text = path.read_text(encoding='utf-8')

old_import = 'import shopping\nimport shopping_official\n'
new_import = 'import shopping\nimport shopping_official\nimport shopping_list_v104\n'
if old_import not in text:
    raise SystemExit('gateway import anchor not found')
text = text.replace(old_import, new_import, 1)

old_block = '''        if path == "/api/shopping/deal-add":
            try:
                payload = self.read_json()
                item = shopping.add_item({
                    "name": payload.get("name", ""),
                    "quantity": payload.get("quantity", ""),
                    "preferred_store": payload.get("preferred_store") or payload.get("store") or "any",
                })
                shopping_official.attach_item(item.get("id"), payload)
                self.send_json({"item": item}, 201)
            except (ValueError, TypeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            return
'''

new_block = '''        if path == "/api/shopping/deal-add":
            try:
                payload = self.read_json()
                item, merged, match_score = shopping_list_v104.add_or_attach_deal(payload)
                shopping_official.attach_item(item.get("id"), payload)
                self.send_json({"item": item, "merged": merged, "match_score": match_score}, 200 if merged else 201)
            except (ValueError, TypeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            return
'''

if old_block not in text:
    raise SystemExit('gateway deal-add anchor not found')
text = text.replace(old_block, new_block, 1)
path.write_text(text, encoding='utf-8')
