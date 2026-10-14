# Alpha research notes

## A trap in the export step

The export step silently drops rows past the first ten thousand unless the batch flag is set.

## Background

The alpha service exports nightly reports to the archive bucket.
