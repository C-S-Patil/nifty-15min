# Excel export fix

The Streamlit error was caused by timezone-aware `EntryTime`/`ExitTime` values.
Excel/XlsxWriter cannot write timezone-aware datetimes.

The export function now:
- strips timezone metadata only in the Excel export copy;
- preserves IST timestamps everywhere else in the application;
- handles object columns containing Timestamp values;
- freezes the header row;
- adds an autofilter;
- applies safe column widths.

No trading logic is changed by this fix.
