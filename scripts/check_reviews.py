"""自店の商品に付いたレビューの増減を毎日見て、変化を知らせる。

## 楽天にレビューAPIは無い

レビュー本文を取るAPIは公開されていない。商品ページのHTMLにも入っていない
（JavaScript で後から描画される。2026-09-04 実測）。

**だが商品検索APIが `reviewCount` と `reviewAverage` を返す。**

    reviewCount = 109 / reviewAverage = 4.35 / shopCode = 'asamu'

本文は読めないが、**「レビューが付いた」「平均が下がった」は分かる。**
店主が知りたいのはまずそこで、本文はRMSで読める。

## 何を知らせるか

  新しいレビューが付いた   件数が増えた
  平均が下がった           対応が要る合図。**先に知りたいのはこちら**
  平均が上がった           そのまま伸ばせる

**平均が下がったものを先に並べる。** 良い知らせから並べると、
悪い知らせが下に埋もれる。悪いほうが急ぐ。

## 使い方

    python scripts/check_reviews.py

    config/shops.json  見る店（楽天の shopCode）
    data/reviews.json  日ごとの件数と平均

環境変数:
  RAKUTEN_ICHIBA_APP_ID / RAKUTEN_ICHIBA_ACCESS_KEY / RAKUTEN_AFFILIATE_ID
  SITE_URL   Referer に使うドメイン。**楽天のアプリで許可済みのものにする**
             （違うと 403 HTTP_REFERRER_NOT_ALLOWED で全件0になる）
  MAX_PAGES  1店あたり何ページ見るか（1ページ30件・既定 10）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config' / 'shops.json'
STORE = ROOT / 'data' / 'reviews.json'

ENDPOINT = 'https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701'
HITS = 30
PAUSE = 1.1


def load(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return fallback


def fetch(url, site, tries=4, wait=4.0):
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; shop-review/1.0)',
                # **Referer と Origin を付ける。** 楽天のアプリ登録の
                # Allowed websites に無いドメインから叩くと 403 になる。
                'Referer': site, 'Origin': site,
            })
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode('utf-8', 'replace'))
        except urllib.error.HTTPError as error:
            body = ''
            try:
                body = error.read().decode('utf-8', 'replace')[:140]
            except Exception:
                pass
            if error.code not in (429, 500, 502, 503):
                print(f'    {error.code}: {body}', file=sys.stderr)
                return {}
            time.sleep(wait * (attempt + 1))
        except Exception as error:
            if attempt == tries - 1:
                print(f'    あきらめます: {error}', file=sys.stderr)
                return {}
            time.sleep(wait * (attempt + 1))
    return {}


def main():
    app_id = os.environ.get('RAKUTEN_ICHIBA_APP_ID', '').strip()
    access_key = os.environ.get('RAKUTEN_ICHIBA_ACCESS_KEY', '').strip()
    affiliate_id = os.environ.get('RAKUTEN_AFFILIATE_ID', '').strip()
    site = os.environ.get('SITE_URL', 'https://chutonavi.jp').rstrip('/')

    if not app_id or not access_key:
        print('RAKUTEN_ICHIBA_APP_ID と RAKUTEN_ICHIBA_ACCESS_KEY が要ります。', file=sys.stderr)
        return 1

    config = load(CONFIG, None)
    if not config:
        print(f'{CONFIG} がありません。', file=sys.stderr)
        return 1

    max_pages = int(os.environ.get('MAX_PAGES') or 10)
    today = date.today().isoformat()

    store = load(STORE, {'items': {}})
    items = store.get('items') or {}
    changes = []

    for shop in config.get('shops', []):
        code = shop['shopCode']
        name = shop.get('name') or code
        print(f'{name}（{code}）', file=sys.stderr)
        seen = 0

        for page in range(1, max_pages + 1):
            query = urllib.parse.urlencode({
                'format': 'json', 'formatVersion': 2,
                'applicationId': app_id, 'accessKey': access_key,
                'affiliateId': affiliate_id,
                'shopCode': code, 'hits': HITS, 'page': page,
                'sort': '-reviewCount',
            })
            payload = fetch(f'{ENDPOINT}?{query}', site)
            time.sleep(PAUSE)

            rows = payload.get('Items') or []
            if not rows:
                break

            for raw in rows:
                item = raw.get('Item', raw)
                item_code = str(item.get('itemCode') or '')
                if not item_code:
                    continue

                seen += 1
                count = int(item.get('reviewCount') or 0)
                average = float(item.get('reviewAverage') or 0)

                before = items.get(item_code)
                items[item_code] = {
                    'shop': code, 'name': str(item.get('itemName') or '')[:120],
                    'url': str(item.get('itemUrl') or ''),
                    'count': count, 'average': average, 'date': today,
                }

                if not before:
                    continue

                added = count - int(before.get('count') or 0)
                moved = round(average - float(before.get('average') or 0), 2)

                if added or moved:
                    changes.append({
                        'date': today, 'shop': code, 'shopName': name,
                        'item': item_code, 'name': items[item_code]['name'],
                        'url': items[item_code]['url'],
                        'added': added, 'moved': moved,
                        'count': count, 'average': average,
                    })

            if len(rows) < HITS:
                break

        print(f'  {seen}件を見ました。', file=sys.stderr)

    # **平均が下がったものを先に並べる。** 悪い知らせのほうが急ぐ。
    changes.sort(key=lambda row: (row['moved'], -row['added']))

    history = store.get('changes') or []
    history.extend(changes)

    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps({'confirmedOn': today, 'items': items,
                                 'changes': history}, ensure_ascii=False), encoding='utf-8')

    print(f'\n変化 {len(changes)}件 / 見ている商品 {len(items):,}件', file=sys.stderr)
    for row in changes[:10]:
        move = f'平均 {row["moved"]:+.2f}' if row['moved'] else '平均そのまま'
        add = f'レビュー +{row["added"]}' if row['added'] else ''
        print(f'  {move} {add}  {row["name"][:40]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
