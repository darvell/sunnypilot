# Gen3 Crosstrek stop-and-go research

Both paths remain locked. This branch is not an install release and does not establish working automatic resume or direct longitudinal control on a vehicle.

## EyeSight-on first

`stop_and_go_research.py --mode eyesight-on <local-rlog>` reconstructs stock HOLD, engagement, gear, physical pedals, wheel speeds and stock lead motion from real CAN. It emits annotations only. It never builds a throttle message, changes Panda permissions or connects to a vehicle.

A candidate requires fresh, checksum-valid, progressing physical messages; adaptive cruise engaged in Drive; all wheel speeds zero; dashboard HOLD; no pedal, steering/ACC fault, soft-disable or camera-announced button action; and a positively observed lead. A bounded lead-distance increase in the existing 3-4.5 m research region must persist across fresh samples. Gas/brake/button input vetoes the rest of that HOLD episode. Lost inputs, lead loss, bad counters and source changes reset the comparison. These thresholds are research policies, not established receiver or safety limits.

An annotation is not a validated means of making the car move. Stock throttle/brake traffic also exists on bus 1, so a proposed injection there would have competing ownership. Panda accepting a frame would not prove that EyeSight uses it correctly. No bus-1 throttle/brake injection permission has been added.

## EyeSight-off option

`stop_and_go_research.py --mode eyesight-off <local-rlog>` audits the recorded request, actual positive/negative CommunicationControl response, engine-off silence interval and subsequent crank. Replies from TX-return/rejection sources never count as physical replies. Successful synthetic observations still do not establish road readiness, button semantics or recovery.

The real startup path remains behind `GEN3_LONGITUDINAL_READY = False`. Its defensive checks now:

- Require an uninterrupted parked engine-off observation from independently sourced throttle/RPM, transmission, steering status, wheel speed, brake and cruise-main traffic. Before intervention, factory actuation streams must also be observed.
- Reject empty receives, missing/stale streams, bad checksums/counters, transient crank/motion/pedal input and steering faults. A final healthy snapshot cannot erase an earlier failure.
- Recheck engine-off conditions during the actual extended-session exchange, before allowing the CommunicationControl request. Make only one disable attempt.
- Require positive `68 03` and a new receive-qualified silence interval. A timeout, ambiguous ownership, returned factory traffic or receive loss does not select stock CarParams as a supposed fallback.
- Stop startup on uncertainty. At runtime, factory traffic returning is an unavailable state, not a demonstrated stock handover. The current process-exit boundary is retained and no automatic Panda-mode transition or hot recovery is claimed.

`CarInterface.init` cannot bypass the qualified Gen3 startup. The existing `deinit` argument-shift bug is corrected: the helper requests enable (`28 00 01`) and requires the corresponding response. This helper is not automatically called and does not demonstrate physical factory-control restoration.

Keeping EyeSight disabled through engine start, independent physical Set/Resume/Cancel inputs after suppression, retained factory functions, actuation calibration and safe failure recovery remain separate required evidence. No ordinary parameter or CLI option unlocks them here.

## Offline usage

From the source environment:

```sh
python -m scripts.subaru_research.stop_and_go_research --mode eyesight-on /path/to/local-rlog.zst
python -m scripts.subaru_research.stop_and_go_research --mode eyesight-off /path/to/local-rlog.zst
```

Only complete local files are accepted. Decode warnings are errors; corrupt/truncated input is not silently treated as a complete observation. Each JSON report always contains `modeLocked: true`, `transmitEnabled: false`, and `roadReadinessEstablished: false`. Any candidate counts are receive-only hypotheses, including when a log has reported Panda faults.

## Reproduced evidence

- Routes 25/0 and 26/0: 6,034 and 4,863 CAN envelopes respectively, zero qualified HOLD episodes and zero resume annotations. Both report the existing interrupt fault; neither is successful-resume evidence.
- Route 28/0: the actual `7f 28 22` rejection is recovered, no qualified engine-off baseline, no positive disable acknowledgement and no silence-through-crank evidence. Existing relay and interrupt faults remain reported.
- Regression: the old empty-receive helper returns true and fails the new empty-receive test. The new tests exercise actual DBC packing/parsing, real ISO-TP handling of positive/rejected/mismatched replies, and a compressed capnp log round trip. Diagnostic traffic during tests exists only in the in-process transport fixture.
- Independent review reproduced absent-event receive-blackout and same-batch fault/clear false positives in the initial analyzer. Both are corrected with behavioral regressions; previous timestamps are checked before overwrite and every relevant fault frame is inspected.
- Full opendbc run: 9,658 tests, 1,270 skipped, no failures. Combined startup/research/controller/safety/MADS/parameter run: 563 tests, 29 skipped, no failures. Preserved passive-intake/readiness/process-characterization run: 38 tests, no failures. SCons minimal build succeeds; type checking for opendbc and Ruff on changed files pass.

These checks establish offline behavior only. A parked physical session and subsequent separately reviewed controlled validation remain necessary before either path becomes a driving feature.
