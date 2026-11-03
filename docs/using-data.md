# Using collected data

The portal supports interactive exploration, retrieval of original files and bounded
programmatic queries. These modes answer different scientific needs: the browser is
suited to discovery and visual inspection; `/api/query` provides JSON-shaped records
for analysis; `/api/download` preserves the collector's original hourly CSV files for
reproducible bulk processing.

## Obtain access and an API token

1. Request an account from **Request access** and complete the single-use onboarding link.
2. Ask an administrator for Browse rights to the required stations and **Can API**.
   Request Download rights as well if original ZIP/CSV retrieval is needed.
3. Sign in, open the username menu, and choose **API tokens**.
4. Create a descriptive token, optionally with an expiry. Copy the `snp_…` secret
   immediately: it is displayed once and only its SHA-256 hash is stored.
5. Keep the secret in an environment variable or secret manager, never source code,
   notebooks committed to Git, URLs, screenshots or shared logs. Revoke it when unused.

Tokens inherit the owner's current permissions. Disabling the user, removing **Can API**,
revoking the token or reaching its expiry stops access immediately.

## Query semantics

`GET /api/query` requires `station`; it accepts comma-separated stations, inclusive
`from`/`to` ISO-8601 bounds, comma-separated `fields`, and a per-station `limit` from
1 to 10,000. Naive timestamps are interpreted as UTC. Stored CSV values remain JSON
strings, so analysis code should explicitly convert numeric fields and handle missing
or malformed observations.

### Python example

This example uses only the Python standard library:

```python
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base_url = "https://portal.example.org"
params = urlencode(
    {
        "station": "station-a",
        "from": "2026-10-01T00:00:00Z",
        "to": "2026-10-02T00:00:00Z",
        "fields": "TempOut,HumOut,RainRate",
        "limit": 10000,
    }
)
request = Request(
    f"{base_url}/api/query?{params}",
    headers={"Authorization": f"Bearer {os.environ['SENSOR_NETWORK_TOKEN']}"},
)
with urlopen(request, timeout=30) as response:
    payload = json.load(response)

rows = payload["stations"][0]["rows"]
temperatures = [float(row["TempOut"]) for row in rows if row.get("TempOut")]
print(f"received {len(rows)} rows; mean temperature={sum(temperatures) / len(temperatures):.2f}")
```

### JavaScript / Node.js example

Node.js 18 and later provide `fetch` and `URL` globally:

```javascript
const baseUrl = 'https://portal.example.org';
const url = new URL('/api/query', baseUrl);
url.search = new URLSearchParams({
  station: 'station-a',
  from: '2026-10-01T00:00:00Z',
  to: '2026-10-02T00:00:00Z',
  fields: 'TempOut,HumOut,RainRate',
  limit: '10000'
});

const response = await fetch(url, {
  headers: {Authorization: `Bearer ${process.env.SENSOR_NETWORK_TOKEN}`}
});
if (!response.ok) throw new Error(`query failed: ${response.status} ${await response.text()}`);
const payload = await response.json();
const rows = payload.stations[0].rows;
const temperatures = rows.flatMap((row) => row.TempOut ? [Number(row.TempOut)] : []);
console.log({rows: rows.length, mean: temperatures.reduce((a, b) => a + b, 0) / temperatures.length});
```

## Download original hourly CSV files

Use `/api/download` with the same `station`, `from` and `to` conventions. The response
is `sensor-data.zip`. Boundary hourly files are included whole rather than rewritten,
so filter individual timestamps after extraction.

```bash
curl --fail --location \
  -H "Authorization: Bearer $SENSOR_NETWORK_TOKEN" \
  --output sensor-data.zip \
  'https://portal.example.org/api/download?station=station-a&from=2026-10-01T00:00:00Z&to=2026-10-02T00:00:00Z'
```

## Reproducible-use checklist

- Record portal URL, station UUIDs, exact UTC interval, fields and retrieval date.
- Preserve original ZIP/CSV products or a checksum when analysis must be repeatable.
- Treat gaps, duplicated timestamps, changing schemas and sensor sentinels explicitly.
- Retain units and station/site metadata with derived tables and figures.
- Do not publish bearer tokens or personal/account metadata.
- Cite the network and relevant software repositories according to project policy.

For schemas, error codes, public snapshots, token CRUD and workflow CRUD, see
[Workflows and data API](workflows-api.md).

## References

1. CCMMMA, [sensor-network-collector documentation](https://github.com/CCMMMA/sensor-network-collector/tree/main/docs).
2. CCMMMA, [sensor-network-pwa](https://github.com/CCMMMA/sensor-network-pwa).
