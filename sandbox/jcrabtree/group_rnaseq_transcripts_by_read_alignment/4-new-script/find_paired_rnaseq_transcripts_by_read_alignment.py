#!/usr/bin/env python3

"""

Proposed replacement for the transcript-pairing portion of group_rnaseq_transcripts_by_read_alignment.py
(https://github.com/jorvis/biocode/blob/master/sandbox/jorvis/group_rnaseq_transcripts_by_read_alignment.py)

Key changes relative to the original script:

1.  Nested dict replaced with a single dict using a composite key, transcript1 + ":" + transcript2
2.  Query coords are not computed since they aren't used
3.  Alignment coverage for each pair of linked transcripts is stored as a sorted list of nonoverlapping
    intervals and updated as new alignments are found. Once a pair of linked transcripts meets the 
    linking criteria (num linking alignments and min bp coverage) it is printed, and *all further 
    alignments linking those 2 transcripts are ignored* (this is the critical improvement.)
4.  Use >= instead of > for the minimum bp coverage in order to be consistent with args.min_mate_pair_count
    (and generally understood convention for what it means to specify a minimum integer threshold.)
5.  Removed incorrect and nondeterministic transitive closure code. This is now handled in a separate
    script using a standard graph algorithm library.
6.  Added min_bp_coverage as parameter, but left the default at 250, the value hard-coded in meets_coverage
    in the original script.
7.  Eliminated 40kb maximum transcript size limit (as a consequence of 3.)
8.  Made --input_sam_file argument optional: the script reads input from stdin if no file is specified.
    This allows piping in the output of "samtools view file.bam", which should reduce file IO at the
    cost of some additional CPU usage.

Unresolved issues/questions:

1.  Does it matter that any indels or mismatches in the CIGAR string are being ignored? Probably not, if
    the original tophat2 search was sufficiently stringent.
2.  A related question: does it matter that we're only checking the coverage on _one_ of the two transcripts?
    The first one to appear in the SAM/BAM file, since the following line in the original script ensures
    that we ignore the other read in the pair:

        if ref_read_base not in pairings[transcripts[0]][transcripts[1]]:

Description from the original script:

This script was created after doing RNA-Seq assemblies for the Aplysia project, in which we
had many reference transcripts generated by other means that we attempted to query within
the assembled transcript set, with varying levels of success.  There were many instances
where the transcripts were fragmented quite a lot compared with the reference (and these were
PCR-validated in our source material.)

We wanted another strategy of grouping these transcripts together in the absence of a reference
genome, so this is an attempt to do so using alignment of the source reads back onto the Trinity-
assembled transcripts using tophat2.  Specifically, this script parses the resulting
'accepted_hits.sam' file and looks for mate pairs spanning different contigs.

Reminder: SAM format:
1   QNAME   String     [!-?A-~]{1,255} Query template NAME
2   FLAG    Int        [0,216-1] bitwise FLAG
3   RNAME   String     \*|[!-()+-<>-~][!-~]* Reference sequence NAME
4   POS     Int        [0,231-1] 1-based leftmost mapping POSition
5   MAPQ    Int        [0,28-1] MAPping Quality
6   CIGAR   String     \*|([0-9]+[MIDNSHPX=])+ CIGAR string
7   RNEXT   String     \*|=|[!-()+-<>-~][!-~]* Ref. name of the mate/next read (* unavailable) (= same)
8   PNEXT   Int        [0,231-1] Position of the mate/next read
9   TLEN    Int        [-231+1,231-1] observed Template LENgth
10  SEQ     String     \*|[A-Za-z=.]+ segment SEQuence
11  QUAL    String     [!-~]+ ASCII of Phred-scaled base QUALity+33

WARNING:
There are a lot of conventions for read naming with regard to direction.  Here in the SAM any
of the following are common:

  HWI-D00688:13:C6F54ANXX:7:2111:15500:46809
  HWI-D00688:13:C6F54ANXX:7:2111:15500:46809/1
  HWI-D00688:13:C6F54ANXX:7:2111:15500:46809__1

It's only the base that we care about here, so if the read IDs end with /N or __N those parts will
be stripped.

NOTES:

Tophat2 does put reciprocals in the output file (columns 1,3,7):

  HWI-D00688:13:C6F54ANXX:7:2111:15500:46809__2	c100009_g1_i1	c78654_g1_i1
  HWI-D00688:13:C6F54ANXX:7:2111:15500:46809__1	c78654_g1_i1	c100009_g1_i1

"""

import argparse
import os
import re
import sys
import pprint

# globals
min_bp_coverage_default = 250

# Add coverage from a single alignment to a transcript pair.
#
# pdict - dict of coverage info keyed by transcript1 + ":" + transcript2
# key - composite key of the form transcript1 + ":" + transcript2
# rs - reference start position for the alignment
# re - reference end position for the alignment
# 
# returns (n_reads, covered_bp), the total number of reads linking the transcripts seen so
# far, and the cumulative basepair coverage of the same
#
def add_read_coverage(pdict, key, rs, re):
    # for each linked transcript pair, pdict stores a list of nonoverlapping intervals of
    # the form (read_count, rstart, rend) sorted by ascending start coordinate (rstart)
    # read_count is the number of alignment pairs contributing to that interval
    
    # case 1: this is the first alignment/read spanning the 2 transcripts
    if key not in pdict:
        pdict[key] = [(1, rs, re)]
        total_bp = re - rs + 1
        # this alignment is all the coverage we have:
        return (1, total_bp)

    # case 2: this is not the first read spanning the 2 transcripts
    interval_list = pdict[key]
    ill = len(interval_list)

    # insert rs, re into the (sorted) list
    insert_done = False
    for i in range(0, ill):
        il = interval_list[i]
        n_reads, istart, iend = il

        # case 2a: rs, re comes strictly before istart, iend
        if re < istart:
            interval_list.insert(i, (1, rs, re))
            insert_done = True
            break

        # case 2b: rs, re comes strictly after istart, iend
        elif rs > iend:
            continue

        # case 2c: rs, re overlaps with istart, iend (or is contained within it) 
        # and needs to be merged with the existing interval
        else:
            starts = sorted([rs, istart])
            ends = sorted([re, iend])
            interval_list[i] = (1 + n_reads, starts[0], ends[1])
            insert_done = True

    # if the new interval is not yet inserted then it goes at the end of the list
    if not insert_done:
        interval_list.append((1, rs, re))
        
    # calculate updated cumulative read count and coverage
    # NOTE: we could be smarter about tracking these values in a more efficient way,
    # but in practice we're only dealing with small numbers of intervals and transcript pairs
    n_reads = 0
    covered_bp = 0

    for il in interval_list:
        nr, istart, iend = il
        n_reads += nr
        covered_bp += (iend - istart + 1)

    return (n_reads, covered_bp)

def main():
    parser = argparse.ArgumentParser( description='Identifies pairs of transcripts linked by paired-end read alignments.')
    parser.add_argument('-i', '--input_sam_file', type=str, required=False, help='SAM file of read alignments back to transcripts. Reads from stdin if not specified.' )
    parser.add_argument('-mbp', '--min_bp_coverage', type=int, required=False, help='Minimum alignment coverage needed to join/group two fragments, in number of base pairs.', default=min_bp_coverage_default )
    parser.add_argument('-mmpc', '--min_mate_pair_count', type=int, required=True, help='Minimum number of mate pairs spanning two fragments required to group them together.' )
    args = parser.parse_args()

    sys.stderr.write("INFO: min_bp_coverage=" + str(args.min_bp_coverage) + "\n")
    sys.stderr.write("INFO: min_mate_pair_count=" + str(args.min_mate_pair_count) + "\n")

    # for each linked transcript pair, pairings stores a list of nonoverlapping intervals of
    # the form (read_count, rstart, rend) sorted by ascending start coordinate (rstart)
    # read_count is the number of alignment pairs contributing to that interval
    # the key is transcript1 + ":" + transcript2, where transcript1 sorts alphabetically before transcript2
    pairings = {}

    # count reads whose mate aligns to nothing
    single_read_pairings = 0
    # count reads whose mate aligns to the same transcript
    same_read_pairings = 0

    # track and count pairs of transcripts that have already met the selection criteria
    selected_pairings = {}
    n_selected_pairings = 0

    # SAM/BAM lines ignored because they span a pair that is already in selected_pairings
    n_lines_ignored = 0
    n_lines = 0

    # ensure that only one of a read pair is added to the coverage for a transcript pair
    # NOTE: see unresolved issue/question #2 - it's not clear if this is the best approach
    reads_used = {}
    n_reads_already_used = 0

    sys.stderr.write("INFO: parsing SAM file and creating transcript pairings\n")

    # read from stdin if --input_sam_file not specified
    fh = sys.stdin
    if args.input_sam_file != None:
        fh = open(args.input_sam_file)

    for line in fh:
        # skip header lines
        if line[0] == '@': continue
        # count non-header lines
        n_lines += 1

        # print progress every 10 million SAM/BAM lines
        if (n_lines % 10000000) == 0:
            sys.stderr.write("INFO: {0} line(s) processed".format(n_lines) + "\n")
            sys.stderr.flush()

        cols = line.split("\t")
        ref_read = cols[0]
        ref_transcript = cols[2]
        other_transcript = cols[6]

        # we don't care about the lines where the mate is unmapped or mapped to the same transcript
        if other_transcript == '*':
            single_read_pairings += 1
            continue
        elif other_transcript == '=':
            same_read_pairings += 1
            continue

        # construct composite dict key based on transcript names
        transcripts = sorted([ref_transcript, other_transcript])
        key = transcripts[0] + ":" + transcripts[1]

        # this pairing has already been selected/printed - no further computation is required
        if key in selected_pairings:
            n_lines_ignored += 1
            continue

        # get the ref read name without the /1 or /2 or _1/_2
        # NOTE: the ref_read_name variable was originally named ref_read_base. I renamed it due to 
        # potential confusion between the "base" of the name (i.e., the bit without the .1 on the end)
        # and "base" as in the actual sequence (or part of it) of the reference read.
        m = re.match("(.+)__[12]$", ref_read)
        if m:
            ref_read_name = m.group(1)
        else:
            m = re.match("(.+)\/[12]$", ref_read)
            if m:
                ref_read_name = m.group(1)
            else:
                ref_read_name = ref_read

        # calculate reference coords from CIGAR string
        rstart = cols[3]
        cigar = cols[5]

        # reference
        rstart = int(cols[3])
        rlen = 0
        for m in re.finditer("(\d+)[M=XDN]", cigar):
            rlen += int(m.group(1))

        rend = rstart + rlen - 1

#        sys.stderr.write("DEBUG: " + line + "\n")
#        sys.stderr.write("DEBUG: t1:{0} t2:{1} read:{2}, rstart:{3}, rend:{4}\n".format(transcripts[0], transcripts[1], ref_read_name, rstart, rend))

        # ignore the other end of a read that's already been seen for this pair
        # NOTE: see unresolved issue/question #2 - it's not clear if this is the best approach
        rkey = key + ":" + ref_read_name
        if rkey in reads_used:
            n_reads_already_used += 1
            continue

        # add the coverage from this read/alignment to the transcript pair
        rc = add_read_coverage(pairings, key, rstart, rend)
        n_reads, covered_bp = rc

        # determine whether the pair now meets the selection criteria
        if (n_reads >= args.min_mate_pair_count) and (covered_bp >= args.min_bp_coverage):
            # it does - print it out and flag this pair to be ignored in future
            print(key)
            n_selected_pairings += 1
            selected_pairings[key] = True
            # this might save a little memory but isn't critical
            pairings.pop(key, None)
        else:
            # doing this later rather than sooner may save a small amount of time
            reads_used[rkey] = True

    # print summary information
    sys.stderr.write("INFO: There were {0} non-header line(s) total\n".format(n_lines))
    sys.stderr.write("INFO: There were {0} single-read mappings unused\n".format(single_read_pairings))
    sys.stderr.write("INFO: There were {0} same-read mappings unused\n".format(same_read_pairings))
    sys.stderr.write("INFO: There were {0} pairing(s) added\n".format(n_selected_pairings))
    sys.stderr.write("INFO: There were {0} line(s) ignored for already-selected pairs\n".format(n_lines_ignored))
    sys.stderr.write("INFO: There were {0} line(s) ignored for mates of already-used reads\n".format(n_reads_already_used))
    sys.stderr.write("INFO: len(reads_used) = {0}\n".format(len(reads_used)))

if __name__ == '__main__':
    main()
