# openpilot — Ioniq 6 custom build

A personal build of openpilot for a **Hyundai Ioniq 6 (2023–24, non-HDA-II / Highway Driving Assist)** running on a **comma 3X**.

Based on the `release-tizi` stable line (v2026.002.001) with Ioniq 6–specific tuning.

## Install on the device

In **Custom Software**, enter:

```
installer.comma.ai/C4rohan/ioniq6-tizi-custom
```

Requires the Hyundai **"L" harness** (CAN-FD, non-HDA-II).

## What's customized

- Dedicated NNLC (Neural Network Lateral Control) model for the Ioniq 6, shared with the E-GMP platform Ioniq 5.
- Lateral torque tuning aligned to the Ioniq 5 measured fit.

## User data

By default openpilot uploads driving data to comma's servers; you can view it via [comma connect](https://connect.comma.ai/) and disable collection in settings. Logged data includes the road-facing camera, CAN, GPS, IMU, magnetometer, thermal sensors, crashes, and OS logs. The driver-facing camera and microphone are only logged if you opt in.

## Licensing

Released under the [MIT License](LICENSE). This repository contains significant portions of code derived from [openpilot by comma.ai](https://github.com/commaai/openpilot), released under the MIT license with additional disclaimers, reproduced below as required:

> openpilot is released under the MIT license. Some parts of the software are released under other licenses as specified.
>
> Any user of this software shall indemnify and hold harmless Comma.ai, Inc. and its directors, officers, employees, agents, stockholders, affiliates, subcontractors and customers from and against all allegations, claims, actions, suits, demands, damages, liabilities, obligations, losses, settlements, judgments, costs and expenses (including without limitation attorneys' fees and costs) which arise out of, relate to or result from any use of this software by user.
>
> **THIS IS ALPHA QUALITY SOFTWARE FOR RESEARCH PURPOSES ONLY. THIS IS NOT A PRODUCT.
> YOU ARE RESPONSIBLE FOR COMPLYING WITH LOCAL LAWS AND REGULATIONS.
> NO WARRANTY EXPRESSED OR IMPLIED.**

For full license terms, see the [`LICENSE`](LICENSE) file.
