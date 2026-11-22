---
id: KD-1
title: 'example-web: root filesystem is 85% full'
status: Done
assignee:
  - '@sysadmin'
created_date: '2020-01-01 13:40'
updated_date: '2020-01-02 08:15'
labels:
  - 'from:sysadmin'
  - 'server:example-web'
milestone: m-10
dependencies:
  - KD-2
project: admin
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The monitor reports that the root filesystem is 85% full. Identify what is growing and propose cleanup.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 growth source identified
- [ ] #2 threshold checked after cleanup
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
[sysadmin] du under /var shows growing container logs
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
The threshold is now 90%. Container logs were the source of growth. Log rotation is enabled.
<!-- SECTION:FINAL_SUMMARY:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @boss
created: 2020-01-01 13:46
---
[boss] directive d-1: KD-1 depends on KD-2. Continue when it is done.
---

author: @sysadmin
created: 2020-01-01 14:02
---
[sysadmin] reply to directive d-1: acknowledged, waiting for KD-2.
---
The line after the delimiter belongs to the same reply.
---

author: @boss
created: 2020-01-02 08:10
---
[boss] unblock: KD-2 Done
---
<!-- COMMENTS:END -->
