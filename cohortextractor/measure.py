import numpy

SMALL_NUMBER_THRESHOLD = 5


class Measure:
    def __init__(
        self, id, denominator, numerator, group_by=None, small_number_suppression=False
    ):
        """
        Creates a "measure" using data extracted by the StudyDefinition defined
        in the same file.

        Args:
            id: A string used for the output filename (which will be
                `measure_<id>.csv`).  Only alphanumeric and underscore characters
                allowed.
            denominator: A column name from the study definition, or the
                special name "population". Columns must be numeric or boolean.
                For boolean columns the value of the denominator will be the
                number of patients with the value "true". Using the
                "population" denominator will give you the total number of
                patients in the cohort.
            numerator: A column name from the study definition. This must be
                numeric or boolean. For boolean columns the value of the numerator
                will be the number of patients with the value "true".
            group_by: A column name, or a list of column names, from the study
                definition to group results by. Use the special column
                "population" to treat the entire population as a single group.
                Set group_by to None (or omit it entirely) to perform no
                grouping and leave the data at individual patient level.
            small_number_suppression: A boolean to enable or disable
                suppression of small numbers. If enabled, numerator and denominator
                values less than or equal to 5 will be suppressed to
                `numpy.nan` to avoid re-identification. Defaults to `False`.

        Returns:
            Measure instance

        """
        self.id = id
        self.denominator = denominator
        self.numerator = numerator
        if group_by is None:
            self.group_by = []
        elif not isinstance(group_by, (list, tuple)):
            self.group_by = [group_by]
        else:
            self.group_by = group_by
        self.small_number_suppression = small_number_suppression

    def calculate(self, data):
        """
        Calculates this measure on the provided patient dataset.

        Args:
            data: a Pandas DataFrame
        """
        columns = _drop_duplicates([self.numerator, self.denominator, *self.group_by])

        # Ensure we're working on a copy rather than a view so that
        # modifications we make (for example low number suppression)
        # can't be reflected in the underlying data.
        result = data[columns].copy()

        if self.group_by:
            result = result.groupby(self.group_by).sum()
            result = result.reset_index()

        if self.small_number_suppression:
            result.loc[
                _is_suppressible(result[self.numerator]), self.numerator
            ] = numpy.nan
            result.loc[
                _is_suppressible(result[self.denominator]), self.denominator
            ] = numpy.nan

        result["value"] = result[self.numerator] / result[self.denominator]

        return result


def _drop_duplicates(lst):
    """
    Preserves the order of the list.
    """
    return list(dict.fromkeys(lst).keys())


def _is_suppressible(column):
    """
    Args:
        column: a column of a DataFrame
    """
    return (column > 0) & (column <= SMALL_NUMBER_THRESHOLD)
