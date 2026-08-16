# Credits

## py-huckleberry-api

All Huckleberry integration in this project is built on
**[py-huckleberry-api](https://github.com/Woyken/py-huckleberry-api)** by
**[Woyken](https://github.com/Woyken)**, used as an unmodified PyPI dependency
(`huckleberry-api`) under the **MIT Licence**.

The hard part of talking to Huckleberry — determining that its Firebase Security Rules reject
plain REST calls and that the client must speak Firestore over gRPC the way the mobile app
does, and mapping the entire document schema for sleep, feeding, pumping, diapers, solids and
growth — is **their** reverse-engineering work, not ours. This project would not exist without
it.

If you find this bot useful, go star their repository.

```
MIT License

Copyright (c) Woyken

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Contributions sent back upstream

Rather than forking, findings from building this project are reported to the source:

| Finding | Status |
|---|---|
| `FirebaseBottleFeedIntervalData.amount` is required, but real Huckleberry data omits it for amount-less bottle entries — breaks `list_feed_intervals` | to file |
| `list_feed_intervals` wraps both Firestore queries in one `try/except`, so a single unparseable document aborts the rest of the read | to file (discussion) |

## Other dependencies

- [FastAPI](https://github.com/fastapi/fastapi) — MIT
- [Pydantic](https://github.com/pydantic/pydantic) — MIT
- [aiohttp](https://github.com/aio-libs/aiohttp) — Apache 2.0
- [uvicorn](https://github.com/encode/uvicorn) — BSD 3-Clause
- [google-cloud-firestore](https://github.com/googleapis/python-firestore) — Apache 2.0
  (pulled in by `huckleberry-api`)

## Disclaimer

This project is **not affiliated with, endorsed by, or supported by Huckleberry Labs, Inc.**,
nor by Meta Platforms, Inc.

It reaches Huckleberry through an unofficial, reverse-engineered client. That interface can
change or break at any time without notice, and using it may not be consistent with
Huckleberry's terms of service. It is intended for personal use with your own account and your
own data. Use at your own risk.
