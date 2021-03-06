#! /usr/bin/env python
#
# extension/step1.py
#
# Nick Barnes, Climate Code Foundation, 2012-05-27
# Avi Persin, Revision 2016-01-06
#
# Copyright (C) Climate Code Foundation, 2010-2012.
# BSD license, see license.txt

"""
Python code for extensions to the Step 1 part of the GISTEMP
algorithm.  In this step: duplicate records from the same station are
combined into a single record, if possible.  Some particular records
are also corrected under the control of the file config/step1_adjust

This behaviour was rendered redundant by the adoption in GISTEMP v3
of GHCN-M 3 as a source dataset.
"""

import math
import itertools

import parameters
from steps import read_config
from steps import series
from steps.giss_data import valid, invalid, MISSING

comb_log = None
pieces_log = None


def step1_adjust():
    """Reads the file config/step1_adjust into a dict,
    mapping a record identifier to a tuple (year, month, summand).
    By convention the month is 1 for January."""

    adjust = {}
    for line in open('config/step1_adjust', 'r'):
        line = line.split('#')[0].strip()
        if line == '':
            continue
        id, _, year, month, summand = line.split()
        adjust[id] = (int(year), int(month), float(summand))
    return adjust


def comb_records(stream):
    """Combine records for the same station (the same id11) where
    possible.  Records are combined by offsetting based on the average
    difference over their common period, then averaged.  Each combined
    record is yielded.
    """

    return do_combine(stream, comb_log, get_best, combine)


def comb_pieces(stream):
    """comb_pieces() attempts to further combine the records produced
    by comb_records() - which have shorter overlaps - by comparing the
    annual anomalies of the years in which they do overlap, and
    finding ones for which the temperatures (in years which they do
    have in common) are on average closer together than the standard
    deviation of the combined record."""

    return do_combine(stream, pieces_log, get_longest, pieces_combine)


def do_combine(stream, log, select_func, combine_func):
    """Drive record combination.

    This is a filter driver function used by ``comb_records`` and
    ``comb_pieces``.

    :Param stream:
        The stream of records to filter.
    :Param log:
        Open log file file.
    :Param select_func:
        A function to call to select the 'best' record from a collection
        of records (belonging to the same station).
    :Param combine_func:
        A function to call to perform the data combining.

    """
    for id11, record_set in itertools.groupby(stream, lambda r: r.station_uid):
        log.write('%s\n' % id11)
        records = set()
        for record in record_set:
            records.add(record)
            ann_mean, ann_anoms = series.monthly_annual(record.series)
            record.set_ann_anoms(ann_anoms)
            record.ann_mean = ann_mean
        begin, end = records_begin_end(records)
        years = end - begin + 1
        # reduce the collection of records (by combining) until there
        # are none (or one) left.
        while records:
            if len(records) == 1:
                # Just one left, yield it.
                yield records.pop()
                break
            record = select_func(records)
            records.remove(record)
            sums, wgts = fresh_arrays(record, years)
            log.write("\t%s %s %s -- %s\n" % (record.uid,
                                              record.first_valid_year(), record.last_valid_year(),
                                              record.source))
            combine_func(sums, wgts, begin, records, log, record.uid)
            final_data = average(sums, wgts)
            record.set_series(begin * 12 + 1, final_data)
            yield record


def combine(sums, wgts, begin, records, log):
    while records:
        record, diff, overlap = get_longest_overlap(average(sums, wgts),
                                                    begin, records)
        if overlap < parameters.station_combine_min_overlap:
            log.write("\tno other records okay\n")
            return
        records.remove(record)
        offset_and_add(sums, wgts, diff, record)
        log.write("\t %s %d %d %f\n" % (record.uid,
                                        record.first_valid_year(),
                                        record.last_valid_year(), diff))


def get_best(records):
    """Given a set of records, return the "best" one.
    "best" considers the source of the record, preferring MCDW over
    USHCN over SUMOFDAY over UNKNOWN.

    (this is passed to do_combine() as a select_func argument)
    """

    ranks = {'MCDW': 4, 'USHCN2': 3, 'SUMOFDAY': 2, 'UNKNOWN': 1}
    best = 1
    longest = 0
    for record in sorted(records, key=lambda r: r.uid):
        length = record.ann_anoms_good_count()
        rank = ranks[record.source]
        if rank > best:
            best = rank
            best_rec = record
        elif length > longest:
            longest = length
            longest_rec = record
    if best > 1:
        return best_rec
    return longest_rec


def pieces_combine(sums, wgts, begin, records, log, new_id):
    """The combine_func (passed to do_combine()) for comb_pieces().

    Combines remaining records that have insufficient overlap.
    """

    while records:
        record, diff_, overlap_ = get_longest_overlap(average(sums, wgts),
                                                      begin, records)
        log.write("\t %s %d %d\n" % (record.uid,
                                     record.first_valid_year(),
                                     record.last_valid_year()))

        is_okay = find_quintuples(sums, wgts, record, new_id, log)

        if is_okay:
            records.remove(record)
            offset_and_add(sums, wgts, 0.0, record)
        else:
            log.write("\t***no other pieces okay***\n")
            return


def offset_and_add(sums, wgts, diff, record):
    """Add the data from *record* to the *sums* and *wgts* arrays, first
    shifting it by subtracting *diff*.  The arrays and *record* are
    assumd to start with the same year.
    """

    for i, datum in enumerate(record.series):
        if invalid(datum):
            continue
        sums[i] += datum - diff
        wgts[i] += 1


def get_longest(records):
    """Considering the records in the *records* set, return the longest
    one.  This is the select_func (passed to do_combine()) used by
    comb_pieces."""

    def length(rec):
        """Length of a record, according to the number of valid annual
        anomalies."""

        return rec.ann_anoms_good_count()

    t = dict((record.uid, record) for record in records)
    return max(t.values(), key=length)


def find_quintuples(sums, wgts, record, new_id, log):
    """The *sums* and *wgts* arrays are assumed to begin in the same
    year as *record*.  Returns a boolean."""

    # An identifier common to all the log output.
    logid = "%s %s" % (new_id, record.uid)

    rec_begin = record.first_valid_year()
    rec_end = record.last_valid_year()

    actual_begin, actual_end = get_actual_endpoints(wgts, record.first_year)

    max_begin = max(actual_begin, rec_begin)
    min_end = min(actual_end, rec_end)
    # Since max_begin and min_end are integers, this rounds fractional
    # middle years up.
    middle_year = int(.5 * (max_begin + min_end) + 0.5)
    offset = (middle_year - record.first_year)
    log.write("max begin: %s\tmin end: %s\n" % (max_begin, min_end))

    new_data = average(sums, wgts)
    new_ann_mean, new_ann_anoms = series.monthly_annual(new_data)
    ann_std_dev = sigma(new_ann_anoms)
    log.write("ann_std_dev = %s\n" % ann_std_dev)

    rec_ann_anoms = record.ann_anoms
    rec_ann_mean = record.ann_mean

    # Whether we have an "overlap" or not.  We have an "overlap" if
    # within *rad* years either side of *middle_year* both records have
    # *parameters.station_combine_min_mid_year* valid annnual anomalies.
    ov_success = False
    # The overlap is "okay" when the difference in annual temperature is
    # below a certain threshold.
    okay_flag = False
    for rad in range(1, parameters.station_combine_bucket_radius + 1):
        # For the two series, get data from from -rad to rad (inclusive)
        # around the middle year.
        base = offset - rad
        base = max(0, base)
        limit = offset + rad + 1
        new_middle = [x for x in new_ann_anoms[base:limit] if valid(x)]
        rec_middle = [x for x in rec_ann_anoms[base:limit] if valid(x)]
        if (len(new_middle) >= parameters.station_combine_min_mid_years
            and len(rec_middle) >= parameters.station_combine_min_mid_years):
            log.write("overlap success: %s\n" % logid)
            ov_success = True
            avg1 = sum(anom + new_ann_mean for anom in new_middle) / float(
                    len(new_middle))
            avg2 = sum(anom + rec_ann_mean for anom in rec_middle) / float(
                    len(rec_middle))
            diff = abs(avg1 - avg2)
            log.write("diff = %s\n" % diff)
            if diff < ann_std_dev:
                okay_flag = True
                log.write("combination success: %s\n" % logid)
            else:
                log.write("combination failure: %s\n" % logid)
            break
    if not ov_success:
        log.write("overlap failure: %s\n" % logid)
    log.write("counts: %d %d\n" % (len(new_middle), len(rec_middle)))
    return okay_flag


def get_actual_endpoints(wgts, begin):
    """For the array of weights in *wgts* return the first and last
    calendar years that have some weight (contain a month with non-zero
    weight); assuming the array starts in year *begin*."""

    # Exact number of years.
    assert len(wgts) % 12 == 0
    y_min = 9999
    y_max = 0
    for i in range(0, len(wgts), 12):
        if sum(wgts[i:i + 12]) > 0:
            y = i // 12
            y_min = min(y_min, y)
            y_max = max(y_max, y)
    return begin + y_min, begin + y_max


def records_begin_end(records):
    """*records* is a set of records.

    (*year_min*, *year_max*) is returned, where *year_min* and
    *year_max* are the minimum and maximum years with data, across
    all the records consulted.

    This function asserts that all the records have the same first year
    (which will be *year_min*)
    """

    first_years = set(record.first_year for record in records)
    assert 1 == len(first_years)
    y_min = list(first_years)[0]
    y_max = max(record.last_year for record in records)
    return y_min, y_max


def adjust_discont(stream):
    """Modifies records as specified in config/step1_adjust,
    by adding the delta to every datum for that station for the early
    part of the record up to and including the specified month.  The
    month is specified (in the file) as 1-based.

    Recently, this adjusts the record for Lihue and St Helena; in the
    corresponding GISTEMP code there is one function and one config file
    for each station.
    """
    adjust = read_config.step1_adjust()
    for record in stream:
        id = record.uid
        if id in adjust:
            series = record.series
            this_year, month, summand = adjust[id]
            begin = record.first_year
            # Index of month specified by *adjust*.
            m = (this_year - begin) * 12 + month - 1
            # All valid data up to and including M get adjusted.
            for i in range(m + 1):
                datum = series[i]
                if invalid(datum):
                    continue
                series[i] += summand
            record.set_series(record.first_month, series)
            del adjust[id]
        yield record


def average(sums, counts):
    """Divide *sums* by *counts* to make a series of averages.
    Return an array with sums[i]/counts[i], and MISSING where
    counts[i] is zero.
    """

    assert len(sums) == len(counts)

    data = [MISSING] * (len(sums))

    for i, (sum, count) in enumerate(zip(sums, counts)):
        if count:
            data[i] = float(sum) / count

    return data


def sigma(list):
    # Remove invalid (missing) data.
    list = list(filter(valid, list))
    if len(list) == 0:
        return MISSING
    # Two pass method ensures argument to sqrt is always positive.
    mean = sum(list) / len(list)
    sigma_squared = sum((x - mean) ** 2 for x in list)
    return math.sqrt(sigma_squared / len(list))


def get_longest_overlap(target, begin, records):
    """Find the record in the *records* set that has the longest
    overlap with the *target* by considering annual anomalies.  *target*
    is a sequence of monthly values starting in the year *begin*.

    A triple (record, diff, overlap) is returned; *diff* is the average
    difference in annual anomalies between *record* and *target*
    (positive when *record* is higher); *overlap* is the number of years
    in the overlap.  Even when there is no overlap _some_ record is
    returned and in that case *diff* is None and *overlap* is 0.
    
    Like other functions, assumes (and asserts) that *begin* is
    the first year for all the records.
    """

    # Annual mean, and annual anomaly sequence.
    mean, anoms = series.monthly_annual(target)
    overlap = 0
    diff = None
    # :todo: the records are consulted in an essentially arbitrary
    # order (which depends on the implementation), but the order
    # may affect the result.  Tie breaks go to the last record consulted.
    # For exact compatiblity with previous versions, we create a
    # temporary dict.
    t = dict((record.uid, record) for record in records)
    for record in t.values():
        common = [(rec_anom, anom)
                  for rec_anom, anom in zip(record.ann_anoms, anoms)
                  if valid(rec_anom) and valid(anom)]
        if len(common) < overlap:
            continue
        overlap = len(common)
        best_record = record
        s = sum((record.ann_mean + rec_anom) - (mean + anom)
                for rec_anom, anom in common)
        if common:
            diff = s / len(common)
    return best_record, diff, overlap


def fresh_arrays(record, years):
    """Make and return a fresh pair of arrays: (*sums*, *wgts*).
    Each array is list (of length 12 * years; the input record should
    not be longer).

    The start of the result arrays will be the same as the start of the
    input *record*, which should generally be the same for all inputs.
    """

    nmonths = years * 12

    # Number of months in record.
    rec_months = len(record)
    assert rec_months <= nmonths

    sums = [0.0] * nmonths
    # Copy valid data rec_data into sums, assigning 0 for invalid data.
    sums[:rec_months] = (valid(x) * x for x in record.series)
    # Let wgts[i] be 1 where sums[i] is valid.
    wgts = [0] * nmonths
    wgts[:rec_months] = (int(valid(x)) for x in record.series)

    return sums, wgts


def pre_step1(records):
    """Apply whatever extensions we have for GISTEMP step 1, that run
    before the main step 1.  This is currently the record-combination code
    (superceded when GHCN-M 3.1 was adopted as the source dataset) and
    also adjustments to the  St Helena and Lihue records.

    See also post_step1()."""

    if parameters.combine_records:
        print("Extension: combine and adjust records (old GISTEMP step 1).")
        global comb_log, pieces_log
        comb_log = open('log/comb.log', 'w')
        pieces_log = open('log/pieces.log', 'w')
        combined = comb_records(records)
        adjusted = adjust_discont(combined)
        records = comb_pieces(adjusted)
    return records


def post_step1(records):
    """Apply whatever extensions we have for GISTEMP step 1, that run
    after the main step 1.  None at present."""

    return records
