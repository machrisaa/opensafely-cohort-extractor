#!/usr/bin/env python3

"""A cross-platform script to build cohorts, run models, build and
start a notebook, open a web browser on the correct port, and handle
shutdowns gracefully
"""
import cohortextractor
from collections import defaultdict
import csv
import glob
import importlib
import os
import re
import requests
import sys


import base64
from io import BytesIO
from argparse import ArgumentParser
from matplotlib import pyplot as plt
import numpy as np
import pandas
from pandas.api.types import is_categorical_dtype
from pandas.api.types import is_bool_dtype
from pandas.api.types import is_datetime64_dtype
from pandas.api.types import is_numeric_dtype
import yaml

import datetime
import seaborn as sns

from cohortextractor.remotejobs import get_job_logs
from cohortextractor.remotejobs import submit_job

notebook_tag = "opencorona-research"
target_dir = "/home/app/notebook"


def relative_dir():
    return os.getcwd()


def make_chart(name, series, dtype):
    FLOOR_DATE = datetime.datetime(1960, 1, 1)
    CEILING_DATE = datetime.datetime.today()
    img = BytesIO()
    # Setting figure sizes in seaborn is a bit weird:
    # https://stackoverflow.com/a/23973562/559140
    if is_categorical_dtype(dtype):
        sns.set_style("ticks")
        sns.catplot(
            x=name, data=series.to_frame(), kind="count", height=3, aspect=3 / 2
        )
        plt.xticks(rotation=45)
    elif is_bool_dtype(dtype):
        sns.set_style("ticks")
        sns.catplot(x=name, data=series.to_frame(), kind="count", height=2, aspect=1)
        plt.xticks(rotation=45)
    elif is_datetime64_dtype(dtype):
        # Early dates are dummy values; I don't know what late dates
        # are but presumably just dud data
        series = series[(series > FLOOR_DATE) & (series <= CEILING_DATE)]
        # Set bin numbers appropriate to the time window
        delta = series.max() - series.min()
        if delta.days <= 31:
            bins = delta.days
        elif delta.days <= 365 * 10:
            bins = delta.days / 31
        else:
            bins = delta.days / 365
        if bins < 1:
            bins = 1
        fig = plt.figure(figsize=(5, 2))
        ax = fig.add_subplot(111)
        series.hist(bins=int(bins), ax=ax)
        plt.xticks(rotation=45, ha="right")
    elif is_numeric_dtype(dtype):
        # Trim percentiles and negatives which are usually bad data
        series = series.fillna(0)
        series = series[
            (series < np.percentile(series, 95))
            & (series > np.percentile(series, 5))
            & (series > 0)
        ]
        fig = plt.figure(figsize=(5, 2))
        ax = fig.add_subplot(111)
        sns.distplot(series, kde=False, ax=ax)
        plt.xticks(rotation=45)
    else:
        raise ValueError()

    plt.savefig(img, transparent=True, bbox_inches="tight")
    img.seek(0)
    plt.close()
    return base64.b64encode(img.read()).decode("UTF-8")


def preflight_generation_check():
    """Raise an informative error if things are not as they should be
    """
    missing_paths = []
    required_paths = ["codelists/", "analysis/"]
    for p in required_paths:
        if not os.path.exists(p):
            missing_paths.append(p)
    if missing_paths:
        msg = "This command expects the following relative paths to exist: {}"
        raise RuntimeError(msg.format(", ".join(missing_paths)))


def generate_cohort(
    output_dir,
    expectations_population,
    selected_study_name=None,
    index_date_range=None,
    skip_existing=False,
):
    preflight_generation_check()
    study_definitions = list_study_definitions()
    if selected_study_name and selected_study_name != "all":
        for study_name, suffix in study_definitions:
            if study_name == selected_study_name:
                study_definitions = [(study_name, suffix)]
                break
    for study_name, suffix in study_definitions:
        print(f"Generating cohort for {study_name}...")
        _generate_cohort(
            output_dir,
            study_name,
            suffix,
            expectations_population,
            index_date_range=index_date_range,
            skip_existing=skip_existing,
        )


def _generate_cohort(
    output_dir,
    study_name,
    suffix,
    expectations_population,
    index_date_range=None,
    skip_existing=False,
):
    print("Running. Please wait...")
    study = load_study_definition(study_name)

    os.makedirs(output_dir, exist_ok=True)
    for index_date in _generate_date_range(index_date_range):
        if index_date is not None:
            study.set_index_date(index_date)
            date_suffix = f"_{index_date}"
        else:
            date_suffix = ""
        # If this is changed then the glob pattern in `_generate_measures()`
        # must be updated
        output_file = f"{output_dir}/input{suffix}{date_suffix}.csv"
        if skip_existing and os.path.exists(output_file):
            print(f"Not regenerating pre-existing file at {output_file}")
        else:
            study.to_csv(
                output_file,
                expectations_population=expectations_population,
            )
            print(f"Successfully created cohort and covariates at {output_file}")


def _generate_date_range(date_range_str):
    # Bail out with an "empty" range: this means we don't need separate
    # codepaths to handle the range, single date, and no date supplied cases
    if not date_range_str:
        return [None]
    start, end, period = _parse_date_range(date_range_str)
    if end < start:
        raise ValueError(
            f"Invalid date range '{date_range_str}': end cannot be earlier than start"
        )
    dates = []
    while start <= end:
        dates.append(start.isoformat())
        start = _increment_date(start, period)
    return dates


def _parse_date_range(date_range_str):
    period = "month"
    if " to " in date_range_str:
        start, end = date_range_str.split(" to ", 1)
        if " by " in end:
            end, period = end.split(" by ", 1)
    else:
        start = end = date_range_str
    try:
        start = _parse_date(start)
        end = _parse_date(end)
    except ValueError:
        raise ValueError(
            f"Invalid date range '{date_range_str}': Dates must be in YYYY-MM-DD "
            f"format or 'today' and ranges must be in the form "
            f"'DATE to DATE by (week|month)'"
        )
    return start, end, period


def _parse_date(date_str):
    if date_str == "today":
        return datetime.date.today()
    else:
        return datetime.date.fromisoformat(date_str)


def _increment_date(date, period):
    if period == "week":
        return date + datetime.timedelta(days=7)
    elif period == "month":
        if date.month < 12:
            return date.replace(month=date.month + 1)
        else:
            return date.replace(month=1, year=date.year + 1)
    else:
        raise ValueError(f"Unknown time period '{period}': must be 'week' or 'month'")


def generate_measures(output_dir, selected_study_name=None, skip_existing=False):
    preflight_generation_check()
    study_definitions = list_study_definitions()
    if selected_study_name and selected_study_name != "all":
        for study_name, suffix in study_definitions:
            if study_name == selected_study_name:
                study_definitions = [(study_name, suffix)]
                break
    for study_name, suffix in study_definitions:
        print(f"Generating measure for {study_name}...")
        _generate_measures(output_dir, study_name, suffix, skip_existing=skip_existing)


def _generate_measures(output_dir, study_name, suffix, skip_existing=False):
    print("Running. Please wait...")
    measures = load_study_definition(study_name, value="measures")
    files = glob.glob(f"{output_dir}/input{suffix}*.csv")
    if not files:
        print(
            "No matching output files found. You may need to first run:\n"
            "  cohortextractor generate_cohort --index-date-range ..."
        )
        return
    measure_outputs = defaultdict(list)
    for file in files:
        date = _get_date_from_filename(file)
        patient_df = None
        for measure in measures:
            output_file = f"{output_dir}/measure_{measure.id}_{date}.csv"
            measure_outputs[measure.id].append(output_file)
            if skip_existing and os.path.exists(output_file):
                print(f"Not generating pre-existing file {output_file}")
                continue
            # We do this lazily so that if all corresponding output files
            # already exist we can avoid loading the patient data entirely
            if patient_df is None:
                patient_df = _load_csv_for_measures(file, measures)
            measure_df = patient_df[
                [measure.numerator, measure.denominator, measure.group_by]
            ]
            measure_df = measure_df.groupby(measure.group_by).sum()
            measure_df["value"] = (
                measure_df[measure.numerator] / measure_df[measure.denominator]
            )
            measure_df.to_csv(output_file)
            print(f"Created measure output at {output_file}")
    for measure in measures:
        output_file = f"{output_dir}/measure_{measure.id}.csv"
        _combine_csv_files_with_dates(output_file, measure_outputs[measure.id])
        print(f"Combined measure output for all dates in {output_file}")


def _get_date_from_filename(filename):
    match = re.search(r"_(\d\d\d\d\-\d\d\-\d\d)\.csv$", filename)
    return datetime.date.fromisoformat(match.group(1))


def _load_csv_for_measures(file, measures):
    """
    Given a CSV file name and a list of measures, load the file into a Pandas
    dataframe with types as appropriate for the supplied measures
    """
    numeric_columns = set()
    other_columns = set()
    for measure in measures:
        numeric_columns.update([measure.numerator, measure.denominator])
        other_columns.add(measure.group_by)
    # This is a special column which we don't load form the CSV but whose value
    # is always set to 1 for every row
    numeric_columns.discard("population")
    dtype = {col: "str" for col in other_columns}
    for col in numeric_columns:
        dtype[col] = "float64"
    df = pandas.read_csv(file, dtype=dtype, usecols=list(dtype.keys()))
    df["population"] = 1
    return df


def _combine_csv_files_with_dates(filename, input_files):
    """
    Takes a list of CSV files which have dates in their filenames and combines
    them into a single CSV file with an additional "date" column indicating the
    date for each row
    """
    input_files = sorted(input_files)
    with open(input_files[0]) as first_file:
        reader = csv.reader(first_file)
        headers = next(reader)
    with open(filename, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers + ["date"])
        for file in input_files:
            date = _get_date_from_filename(file)
            with open(file) as input_csvfile:
                reader = csv.reader(input_csvfile)
                if next(reader) != headers:
                    raise RuntimeError(
                        f"Files {input_files[0]} and {file} have different headers"
                    )
                for row in reader:
                    writer.writerow(row + [date])


def make_cohort_report(input_dir, output_dir):
    for study_name, suffix in list_study_definitions():
        _make_cohort_report(input_dir, output_dir, study_name, suffix)


def _make_cohort_report(input_dir, output_dir, study_name, suffix):
    study = load_study_definition(study_name)

    df = study.csv_to_df(f"{input_dir}/input{suffix}.csv")
    descriptives = df.describe(include="all")

    for name, dtype in zip(df.columns, df.dtypes):
        if name == "patient_id":
            continue
        main_chart = '<div><img src="data:image/png;base64,{}"/></div>'.format(
            make_chart(name, df[name], dtype)
        )
        empty_values_chart = ""
        if is_datetime64_dtype(dtype):
            # also do a null / not null plot
            empty_values_chart = '<div><img src="data:image/png;base64,{}"/></div>'.format(
                make_chart(name, df[name].isnull(), bool)
            )
        elif is_numeric_dtype(dtype):
            # also do a null / not null plot
            empty_values_chart = '<div><img src="data:image/png;base64,{}"/></div>'.format(
                make_chart(name, df[name] > 0, bool)
            )
        descriptives.loc["values", name] = main_chart
        descriptives.loc["nulls", name] = empty_values_chart

    with open(f"{output_dir}/descriptives{suffix}.html", "w") as f:

        f.write(
            """<html>
<head>
  <style>
    table {
      text-align: left;
      position: relative;
      border-collapse: collapse;
    }
    td, th {
      padding: 8px;
      margin: 2px;
    }
    td {
      border-left: solid 1px black;
    }
    tr:nth-child(even) {background: #EEE}
    tr:nth-child(odd) {background: #FFF}
    tbody th:first-child {
      position: sticky;
      left: 0px;
      background: #fff;
    }
  </style>
</head>
<body>"""
        )

        f.write(descriptives.to_html(escape=False, na_rep="", justify="left", border=0))
        f.write("</body></html>")
    print(f"Created cohort report at {output_dir}/descriptives{suffix}.html")


def update_codelists():
    base_path = os.path.join(os.getcwd(), "codelists")

    # delete all existing codelists
    for path in glob.glob(os.path.join(base_path, "*.csv")):
        os.unlink(path)

    with open(os.path.join(base_path, "codelists.txt")) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            print(line)
            project_id, codelist_id, version = line.split("/")
            url = f"https://codelists.opensafely.org/codelist/{project_id}/{codelist_id}/{version}/download.csv"

            rsp = requests.get(url)
            rsp.raise_for_status()

            with open(
                os.path.join(base_path, f"{project_id}-{codelist_id}.csv"), "w"
            ) as f:
                f.write(rsp.text)


def dump_cohort_sql(study_definition):
    study = load_study_definition(study_definition)
    print(study.to_sql())


def dump_study_yaml(study_definition):
    study = load_study_definition(study_definition)
    print(yaml.dump(study.to_data()))


def load_study_definition(name, value="study"):
    sys.path.extend([relative_dir(), os.path.join(relative_dir(), "analysis")])
    # Avoid creating __pycache__ files in the analysis directory
    sys.dont_write_bytecode = True
    return getattr(importlib.import_module(name), value)


def list_study_definitions(ignore_errors=False):
    pattern = re.compile(r"^(study_definition(_\w+)?)\.py$")
    matches = []
    try:
        analysis_files = os.listdir(os.path.join(relative_dir(), "analysis"))
    except OSError:
        if not ignore_errors:
            raise
        else:
            analysis_files = []
    for name in sorted(analysis_files):
        match = pattern.match(name)
        if match:
            name = match.group(1)
            suffix = match.group(2) or ""
            matches.append((name, suffix))
    if not matches and not ignore_errors:
        raise RuntimeError(f"No study definitions found in {relative_dir()}")
    return matches


def main():
    parser = ArgumentParser(
        description="Generate cohorts and run models in openSAFELY framework. "
    )
    # Cohort parser options
    parser.add_argument("--version", help="Display version", action="store_true")
    subparsers = parser.add_subparsers(help="sub-command help")
    generate_cohort_parser = subparsers.add_parser(
        "generate_cohort", help="Generate cohort"
    )
    generate_cohort_parser.set_defaults(which="generate_cohort")
    generate_measures_parser = subparsers.add_parser(
        "generate_measures", help="Generate measures from cohort data"
    )
    generate_measures_parser.set_defaults(which="generate_measures")
    cohort_report_parser = subparsers.add_parser(
        "cohort_report", help="Generate cohort report"
    )
    cohort_report_parser.set_defaults(which="cohort_report")
    cohort_report_parser.add_argument(
        "--input-dir",
        help="Location to look for input CSVs",
        type=str,
        default="analysis",
    )
    cohort_report_parser.add_argument(
        "--output-dir",
        help="Location to store output CSVs",
        type=str,
        default="output",
    )

    run_notebook_parser = subparsers.add_parser("notebook", help="Run notebook")
    run_notebook_parser.set_defaults(which="notebook")
    update_codelists_parser = subparsers.add_parser(
        "update_codelists",
        help="Update codelists, using specification at codelists/codelists.txt",
    )
    update_codelists_parser.set_defaults(which="update_codelists")
    dump_cohort_sql_parser = subparsers.add_parser(
        "dump_cohort_sql", help="Show SQL to generate cohort"
    )
    dump_cohort_sql_parser.add_argument(
        "--study-definition", help="Study definition name", type=str, required=True
    )
    dump_cohort_sql_parser.set_defaults(which="dump_cohort_sql")
    dump_study_yaml_parser = subparsers.add_parser(
        "dump_study_yaml", help="Show study definition as YAML"
    )
    dump_study_yaml_parser.set_defaults(which="dump_study_yaml")
    dump_study_yaml_parser.add_argument(
        "--study-definition", help="Study definition name", type=str, required=True
    )

    remote_parser = subparsers.add_parser("remote", help="Manage remote jobs")
    remote_parser.set_defaults(which="remote")

    # Remote subcommands
    remote_subparser = remote_parser.add_subparsers(help="Remote sub-command help")
    generate_cohort_remote_parser = remote_subparser.add_parser(
        "generate_cohort", help="Generate cohort"
    )
    generate_cohort_remote_parser.set_defaults(which="remote_generate_cohort")
    generate_cohort_remote_parser.add_argument(
        "--ref",
        help="Tag or branch against which to run the extraction",
        type=str,
        required=True,
    )
    generate_cohort_remote_parser.add_argument(
        "--repo",
        help="Tag or branch against which to run the extraction (leave blank for current repo)",
        type=str,
    )
    generate_cohort_remote_parser.add_argument(
        "--db",
        help="Database to run against",
        choices=["full", "slice", "dummy"],
        nargs="?",
        const="full",
        default="full",
        type=str,
    )
    generate_cohort_remote_parser.add_argument(
        "--backend",
        help="Backend to run against",
        choices=["all", "tpp"],
        nargs="?",
        const="all",
        default="all",
        type=str,
    )

    log_remote_parser = remote_subparser.add_parser("log", help="Show logs")
    log_remote_parser.set_defaults(which="remote_log")

    # Cohort parser options
    generate_cohort_parser.add_argument(
        "--output-dir",
        help="Location to store output CSVs",
        type=str,
        default="output",
    )
    generate_cohort_parser.add_argument(
        "--study-definition",
        help="Study definition to use",
        type=str,
        choices=["all"] + [x[0] for x in list_study_definitions(ignore_errors=True)],
        default="all",
    )
    generate_cohort_parser.add_argument(
        "--temp-database-name",
        help="Name of database to store temporary results",
        type=str,
        default=os.environ.get("TEMP_DATABASE_NAME", ""),
    )
    generate_cohort_parser.add_argument(
        "--index-date-range",
        help="Evaluate the study definition at a range of index dates",
        type=str,
        default="",
    )
    generate_cohort_parser.add_argument(
        "--skip-existing",
        help="Do not regenerate data if output file already exists",
        action="store_true",
    )
    cohort_method_group = generate_cohort_parser.add_mutually_exclusive_group()
    cohort_method_group.add_argument(
        "--expectations-population",
        help="Generate a dataframe from study expectations",
        type=int,
        default=0,
    )
    cohort_method_group.add_argument(
        "--database-url",
        help="Database URL to query (can be supplied as DATABASE_URL environment variable)",
        type=str,
    )

    # Measure generator parser options
    generate_measures_parser.add_argument(
        "--output-dir",
        help="Location to store output CSVs",
        type=str,
        default="output",
    )
    generate_measures_parser.add_argument(
        "--study-definition",
        help="Study definition file containing measure definitions to use",
        type=str,
        choices=["all"] + [x[0] for x in list_study_definitions()],
        default="all",
    )
    generate_measures_parser.add_argument(
        "--skip-existing",
        help="Do not regenerate measure if output file already exists",
        action="store_true",
    )

    options = parser.parse_args()
    if options.version:
        print(f"v{cohortextractor.__version__}")
    elif not hasattr(options, "which"):
        parser.print_help()
    elif options.which == "generate_cohort":
        if options.database_url:
            os.environ["DATABASE_URL"] = options.database_url
        if options.temp_database_name:
            os.environ["TEMP_DATABASE_NAME"] = options.temp_database_name
        if not options.expectations_population and not os.environ.get("DATABASE_URL"):
            parser.error(
                "generate_cohort: error: one of the arguments "
                "--expectations-population --database-url is required"
            )
        generate_cohort(
            options.output_dir,
            options.expectations_population,
            selected_study_name=options.study_definition,
            index_date_range=options.index_date_range,
            skip_existing=options.skip_existing,
        )
    elif options.which == "generate_measures":
        generate_measures(
            options.output_dir,
            selected_study_name=options.study_definition,
            skip_existing=options.skip_existing,
        )
    elif options.which == "cohort_report":
        make_cohort_report(options.input_dir, options.output_dir)
    elif options.which == "update_codelists":
        update_codelists()
        print("Codelists updated. Don't forget to commit them to the repo")
    elif options.which == "dump_cohort_sql":
        dump_cohort_sql(options.study_definition)
    elif options.which == "dump_study_yaml":
        dump_study_yaml(options.study_definition)
    elif options.which == "remote_generate_cohort":
        submit_job(
            options.backend, options.db, options.ref, "generate_cohort", options.repo,
        )
        print("Job submitted!")
    elif options.which == "remote_log":
        logs = get_job_logs()
        print("\n".join(logs))


if __name__ == "__main__":
    main()
