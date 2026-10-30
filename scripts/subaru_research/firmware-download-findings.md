# Subaru firmware acquisition: recovered download paths

## Result

The existing cockpit firmware does contain a real downloader and a configured session origin. The earlier DCM-only account was incomplete. However, the head-unit download path and SSM5 meter/camera `.pfc` acquisition are separate demonstrated workflows; finding one does not establish access to the other.

The missing meter target remains `MET_CH2_CH2RCB0001.pfc`. This investigation recovered acquisition contracts, not that payload. No firmware was run, vehicle contacted, update started, or authentication bypass attempted.

## 1. Downloader actually shipped in the cockpit image

Analyzed local copies of the `FM1WMM073-980` Linux image:

| Binary | SHA256 |
|---|---|
| `soft_vup` | `a2788ebeb8655a8ac5962bbde69b679cb76ed207a4e0cbf364c9193ad11d5b5b` |
| `smm.exe` | `ffe4614d54702885afd27c01c48053f44f7ea063a0dbf979ad47a9b185afc80b` |

These are ELF virtual addresses, not live addresses:

- `soft_vup:0x5A1380` launches `/usr/bin/smm.exe`.
- `soft_vup:0x59D860` requests a Red Bend SCOMO session or forwards a 60-byte DCM campaign notification. The DCM command bridge alone does not establish where package bytes travel.
- `smm.exe:0x50E1EF` triggers an OMA-DM session using the configured account.
- `smm.exe:0x515A91` reads `Download/<package-instance>/PkgURL` from the SCOMO tree; `0x51C42A` constructs that path and calls `VDM_triggerDLSession`. Both were independently decompiled again in the parent session.
- `smm.exe:0x4354A0` implements URL parsing, DNS resolution, socket connection, proxy CONNECT and TLS checks.

The encoded factory management tree was decoded and XML-validated in memory with host tools, without executing firmware. The nonsecret origin at `./DMAcc/callup/AppAddr/APPSRV/Addr` is:

```text
https://prod2nd2018.m45asterope.redbend.com/dm
```

This is a **management-session endpoint**, not a firmware-object URL. It was not contacted. The factory SCOMO/FUMO package URL leaves contain `http://update-package-location`, a placeholder. Runtime session information must provide the selected package instance and actual URL.

The proven acquisition chain is therefore:

```text
configured DM account + runtime head-unit inventory
-> OMA-DM/SCOMO session
-> selected package instance / PkgURL
-> Linux HTTP downloader
-> local package staging
```

`soft_vup:0x521E90` builds `INVENTORY.DAT` from the unit model and integrated version, using the six-character version prefix plus `_Firmware` as `FW_id`. The separate field named `component_id` is `0`; these must not be confused. The observed update-family table at `0x6263E0` contains eight NAVI image families, and observed staging objects include `__SCOMO_RB_DP` and `ALLUPD-NAVI.pkg`.

The shared proxy `http://ndeproxy:8080/` is explicitly supplied as both management and download proxy at `soft_vup:0x59C480`; it is not the origin. Generic Red Bend ECU-inventory support exists, but the supplied ECU inventory CSV is header-only. No mapping from meter/EyeSight identifiers to `.pfc` requests was established. Factory account contents are not proof of a particular vehicle's current provisioning or authorization; credentials, keys and the decoded tree are not exported here.

## 2. Real SSM5 web-client acquisition contracts

The SSM5 host shown in Subaru bulletin 14-28-21R was opened normally. Its public entry page linked the following module, which was downloaded for static inspection, not executed:

```text
Entry: https://ssm5.subaru.com/
Client: https://ssm5.subaru.com/app.1790652540474.js
Client SHA256:
c795588ac39d8312f487de1dfe11026125c7c37dfe83b52f928464fe8946e661
```

The production client uses a `/ph2-rpr` API prefix. When those tokens are present, its request wrapper attaches `Authorization` from an ID token and `x-repro-session-token` from the application session. Recovered read-only flows include:

| Purpose | Relative GET request | Returned/consumed data |
|---|---|---|
| Cloud Connector installer metadata | `/ph2-rpr/netApps` | `version`, `last_update_time`, `path` |
| Server-issued download URL | `/ph2-rpr/downloadUrl?file_path=<authorized catalog path>` | `data.fileUrl` |
| Standalone CCU ZIP catalog | `/ph2-rpr/ccuDownloadUrlList` | package rows including `file_path` |

The installer UI gets its path from metadata before requesting `fileUrl`. The CCU ZIP button is explicitly conditional on `isCCU` and an available file path. ECU choices instead reference server-provided `pfc_file_id` values and a paired Cloud Connector. These are client contracts, not successful authenticated responses. They do not justify inventing a file path from the meter basename or assuming the ZIP button handles meter firmware.

An anonymous request to the exact installer-metadata route returned HTTP 301, then HTTP 403 after following the normal redirect. The latter was a CloudFront "Bad request" page, not a demonstrated application-level authentication response. The rejection was preserved; no alternate host, forged session, role change or bypass was attempted. No catalog or firmware-download call followed it. The two-token/header behavior comes from the shipped client, not an inference about why that 403 occurred.

Public STIS navigation was also inspected through its linked script and ordinary public Special Tool Information API. This is diagnostic-tool acquisition information, not the ECU download service. The live PDI distributor page describes licensed SSM software downloads and a separate process for obtaining aftermarket SSM5 credentials. A normal STIS document subscription must not be assumed to supply those credentials.

## 3. Download is not a safe synonym for starting reprogramming

Subaru bulletin 14-28-21R, revised July 8, 2024, describes `.pfc` retrieval during the reprogramming workflow. The file reaches the laptop before ECU programming, but preparation/programming then follow, and the file is deleted after completion. This is **not** a documented download-only/export button. Do not initiate that workflow merely to capture a file.

We have not established a download-only meter-export operation. Finishing payload retrieval requires an authorized SSM5 session/catalog response or a legitimately obtained `.pfc`/Cloud Connector package suitable for offline inspection. No account was purchased and no message sent to a distributor. No actual meter/camera firmware was downloaded.

## 4. SMM follow-up and the current head-unit release

### Release identity, not just a guessed suffix

As of October 28, 2026, the newest dealer/USB release verified in Subaru's documentation is **RC8**, released **August 17, 2026**, in bulletin 15-305-22R revised August 20, 2026. Its page-31 table explicitly lists these 2024 Impreza/Crosstrek values:

| Variant/component | Documented version |
|---|---|
| High MSOC | `FH1WHM205-280` |
| Mid MSOC | `FH1WMM205-280` |
| Base MSOC | `FH1WBM205-280` |
| VCPU | `F71SHV011-680` |

The actual PDF was downloaded from NHTSA and pages 4, 31 and 41 inspected, with page 31 rendered at readable resolution. Its SHA256 is `72760403ef0c6258ea58b31b1e972e69ad53a5f71c836ce88a6f9feda1dfef6e`. The older RC7/185-180 latest-release note is outdated. In particular, do not manufacture a new full identifier by appending `205-280` to the old `FM1WMM` prefix; the documented current Mid prefix is `FH1WMM`.

The retained F71 payload was rechecked through `vcpu_can_inventory.py`: it reports exactly `F71SHV011-680`, SHA256 `716c0f0f9c3891b70ee4c0e62261f58b591edb1ff6fd1ae999c8d1a97ff78e5b`, with both bank inventories agreeing. Thus the archive already contains the VCPU version listed by that current bulletin. This is version correspondence, not proof that every byte matches a separately acquired RC8 package or that this ECU is installed in the car. The latest head-unit MSOC package itself has not been retrieved.

Subaru's separate FOTA bulletin 15-311-23R, revised July 15, 2026, lists multiple FOTA suffixes and explicitly distinguishes them from dealer/USB releases. An SMM campaign result must not automatically be labeled the RC8 USB package.

The maintainer's currently linked public CP1.5 folder was fetched anonymously and its visible filenames parsed: `FM1WxM073-980.zip` and `15-305-22R_061124.pdf`. No RC7/RC8 filename or version literal appears in that saved listing. This is a check of that specific mirror, not proof no other copy exists.

### Additional update-discovery operations recovered

Continuing past the URL getter established the actual initial request and response mechanism:

- `soft_vup:0x59D860` can initiate a user update check without a DCM campaign notification. That does not prove operation without connectivity, provisioning or account authorization.
- `smm.exe:0x54F809` constructs the client-initiated OMA-DM package with Alert 1201; `0x54F32F` serializes DevInfo as Replace items. A generic Alert 1226 carries the SCOMO request. XML/WBXML format, message/session counters, account auth and nonce state are part of the exchange. This is not a simple model/version GET.
- SCOMO schema table `0x612D00` includes `PkgID`, `Name`, `PkgURL`, `InstallParams`, `Description`, `EnvType`, `PkgType`, integer `Status`, and operation children. Incoming Add/Replace commands supply package-tree state; `0x51C42A` receives a tree path, not a download URL literal. No accepted server response was captured.
- The inventory is ten 32-byte records, each a four-byte length plus 28-byte payload. Its actual vendor is `SBR`; `DENSO` in a getter is an error/test fallback. The fields are model, VIN, vendor, firmware version, firmware ID, component ID, serial, product ID, component-unit flag and component address.

The parent trace also resolved the previously missing DevId producer. `soft_vup:0x59D860` and `0x59EF60` handle `B2D_GET_DEV_ID` by calling `Rpvcmn_get_vin:0x555810`, then `0x599900` sends those bytes as `D2B_DEV_ID_RESPONSE` / `DMA_VAR_DEV_ID`. SMM handler `0x446E0C` passes that value to `DMA_redbend_SESS_devIdSyncDone:0x477020`, which copies it to `g_devId`; callback `0x4F57C5` supplies it for `./DevInfo/DevId`. **On this inspected path, DevId is VIN-sourced.** No actual VIN was read or exported.

The model remains a separate hardware identifier. `soft_vup:0x4CE5C0` obtains a ten-character device-model/part identifier and derives a serial from that model plus a MAC-derived decimal value. `0x4DE980` caches them separately from VIN. The fallback part number and zero-like MAC data are not evidence of the actual unit; they must not be used as a fabricated live profile.

A single credential-free HEAD request to the exact configured Red Bend `/dm` origin completed with **HTTP 405** and successful TLS verification. This establishes an HTTP response at that location, not a successful OMA-DM session, authorization, package catalog or current campaign. No authenticated request, VIN submission, fabricated inventory, bootstrap enrollment, update request, or install was sent.

The exact remaining acquisition operation is a successful authorized client-initiated DM exchange with the actual runtime profile/account, producing a real SCOMO package URL, or a legitimately obtained package/session response. The server-selected URL cannot be calculated by substituting a firmware filename into the factory placeholder. The release identity is now verified; the RC8 package bytes are still unavailable in the examined sources.

## Sources and retained proof

```text
Subaru SSM5 procedure, 14-28-21R, pp. 3, 6 and 8:
https://static.nhtsa.gov/odi/tsbs/2024/MC-11005590-0001.pdf
Current CP1.5 dealer/USB release bulletin, 15-305-22R revised August 20, 2026:
https://static.nhtsa.gov/odi/tsbs/2026/MC-11036803-0001.pdf
Current reviewed FOTA bulletin, 15-311-23R revised July 15, 2026:
https://static.oemdtc.com/NHTSA-PDFs/MC-11035631-0001.pdf
Public mirror maintainer and the specific folder inspected:
https://www.gen6firmware.com/2020/07/2020-subaru-outback-504-update.html
https://drive.google.com/drive/folders/1934DeFCbt_HCfl5JaINI8qkyyYCvcT8G?usp=sharing
Official STIS:
https://techinfo.subaru.com/stis/
Public Special Tool Information route, resolved from STIS's actual client:
https://techinfo.subaru.com/stis/service/public/static_content/special_tool?locale=en
Subaru's aftermarket distributor, current access instructions:
https://security.pditechnologies.com/subaru-tech/
```

Local working evidence retains input hashes, address-backed firmware findings, the fetched public entry pages/client scripts, bounded client-code inspection tools, the public STIS response, and the actual anonymous metadata error. None of the proprietary binaries, full site scripts, account data, tokens or decoded management-tree contents are included in this repository. This report establishes where and how acquisition is negotiated, not a public unauthenticated firmware URL or on-vehicle update verification.
