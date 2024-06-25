import numpy as np
import pandas as pd
import seaborn as sns
import random

from traitlets import Int, List, Bool, CFloat, Unicode

from sepal_ui.model import Model

import component.parameter.app as cp
import component.scripts as cs
import component.parameter as param

import ee
import json
from geopandas import GeoDataFrame


class BasinModel(Model):

    lat = CFloat(0, allow_none=True).tag(sync=True)
    lon = CFloat(0, allow_none=True).tag(sync=True)

    years = List([2010, 2020]).tag(sync=True)
    thres = Int(80).tag(sync=True)

    level = Int(8).tag(sync=True)
    "int: target level of the catchment"

    method = Unicode("").tag(sync=True)
    "Unicode: Selection basin id method (all - filter)"

    selected_hybas = List([]).tag(sync=True)
    "list: current selected hybasid(s) from the dropdown base list"

    hybasin_list = List().tag(sync=True)
    "list: hybasin id list of upstream catchments"

    manual = Bool(False).tag(sync=True)
    "bool: wheter to set the coordinates manually or not"

    marker = Bool(False).tag(sync=True)
    "bool: whether a marker (AOI) is set or not"

    # Statistics
    ready = Bool(False).tag(sync=True)

    sett_timespan = List([2010, 2020]).tag(sync=True)
    "list: user selected span of time in the statistics settings panel"

    selected_var = Unicode("").tag(sync=True)
    "str: current selected variable from pie chart or variable selector widget"

    selected_hybasid_chart = List([]).tag(sync=True)
    "list: selected hybasid(s) from the statistics dashboard list or from catchments pie"

    def __init__(self):
        """

        Params:
            base_basin (ee.FeatureCollection): basin corresponding to the filtered
                wwf hydrosheds at the given aoi and to the given level.
            upstream_catchment (ee.FeatureCollection): upstream catchments from
                the given point at the given level using the base_basin.
            forest_change (ee.Image): forest change mas within the given upstream
                catchments at the given livel using the base basin.
            data (dict): upstream catchments in a geojson format
            zonal_df (df): Zonal statistics dataframe
        """

        self.base_basin = None
        self.upstream_catchs = None
        self.forest_change = None

        self.lat_link = False
        self.lon_link = False

        self.data = None
        self.zonal_df = None

    def get_upstream_basin_ids(self, geometry, max_steps=100):
        """Return a list with all uperstream catchments ids from the base basin

        Args:
            geometry (ee.Geometry): geometry to filter the base catchment level
            max_steps (int) : Arbritrary number to loop over the collection

        Params:
            level (int): WWF catchment level to query the inputs

        """

        def get_upper(i, acc):

            acc = ee.List(acc)

            # get the last accumulated element, (can be a fc with lots of features)
            feature_collection = ee.FeatureCollection(acc.get(acc.size().subtract(1)))

            # we will retrieve all the HYBAS_ID's from the last element
            base_ids = feature_collection.aggregate_array("HYBAS_ID")

            # We will query what are the upstream features from the above ones
            upper_catchments = self.base_basin.filter(
                ee.Filter.inList("NEXT_DOWN", base_ids)
            )

            # and append them into the feature collection (to start again)
            return acc.add(upper_catchments)

        self.base_basin = cs.get_hydroshed(level=self.level)

        upstream_catchs = ee.FeatureCollection(
            ee.List(
                ee.List.sequence(1, max_steps).iterate(
                    get_upper, [self.base_basin.filterBounds(geometry)]
                )
            ).iterate(
                lambda fc, acc: ee.FeatureCollection(acc).merge(
                    ee.FeatureCollection(fc)
                ),
                ee.FeatureCollection([]),
            )
        )

        self.hybasin_list = upstream_catchs.aggregate_array("HYBAS_ID").getInfo()

    def get_upstream_fc(self):
        """Filter and get upstream catchments"""

        return self.get_selected(self.hybasin_list)

    def get_gfc(self, aoi):
        """Creates a forest change map based on gfw dataset

        Params:
            aoi (ee.Geometry): area of interest to clip the change mask
            iniy (int): the initial year of the analysis
            stpoy (int): end year for the loss
            thres (int): minimum value for the tree cover
        """

        iniy_ = self.years[0] - 2000
        stopy_ = self.years[1] - 2000

        gfc = ee.Image(cp.gfc_dataset).clip(aoi)

        treecov = gfc.select(["treecover2000"])
        lossy = gfc.select(["lossyear"]).unmask(0)
        gain = gfc.select(["gain"])

        forest_change = (
            ee.Image(0)
            .where(treecov.lte(self.thres).And(gain.eq(1)), 50)  # gain V
            .where(treecov.lte(self.thres).And(gain.eq(0)), 30)  # non-forest
            .where(
                treecov.gt(self.thres).And(lossy.lt(iniy_)), 30
            )  # non-forest (lost forest before start date)
            .where(
                treecov.gt(self.thres).And(lossy.gt(stopy_)), 40
            )  # stable forest (forest lost after the dates)
            .where(
                treecov.gt(self.thres)
                .And(gain.eq(1))
                .And(lossy.gte(iniy_))
                .And(lossy.lte(stopy_)),
                51,
            )  # gain+loss
            .where(treecov.gt(self.thres).And(gain.eq(1)).And(lossy.eq(0)), 50)  # gain
            .where(
                treecov.gt(self.thres)
                .And(gain.eq(0))
                .And(lossy.gte(iniy_))
                .And(lossy.lte(stopy_)),
                lossy,
            )  # loss
            .where(
                treecov.gt(self.thres).And(gain.eq(0)).And(lossy.eq(0)), 40
            )  # stable forest
            .selfMask()
        )

        return forest_change

    def get_selected(self, hybas_ids, from_json=False):
        """Return the selected Feature Collection or geojson dict

        hybas_ids (list): hydrobasin id's to calculate statistics.

        """

        if from_json:
            gdf = GeoDataFrame.from_features(self.data["features"])
            return json.loads(gdf[gdf["HYBAS_ID"].isin(hybas_ids)].to_json())

        return self.base_basin.filter(ee.Filter.inList("HYBAS_ID", hybas_ids))

    @staticmethod
    def get_bounds(dataset):
        """Get bounds of the given feature collection"""

        if isinstance(dataset, ee.FeatureCollection):

            ee_bounds = dataset.geometry().bounds().coordinates()
            coords = ee_bounds.get(0).getInfo()
            ll, ur = coords[0], coords[2]
            return ll[0], ll[1], ur[0], ur[1]

        elif isinstance(dataset, dict):

            return list(GeoDataFrame.from_features(dataset["features"]).total_bounds)

    def calculate_statistics(self):
        """Get hydrobasin id statistics on the given hybasin_id

        hybas_ids (list): hydrobasin id's to calculate statistics.

        """

        if self.method == "filter" and not self.selected_hybas:
            raise Exception("Please select a subcatchment.")

        feature_collection = self.base_basin.filter(
            ee.Filter.inList(
                "HYBAS_ID",
                self.selected_hybas if self.method != "all" else self.hybasin_list,
            )
        )

        return (
            ee.Image.pixelArea()
            .divide(10000)
            .addBands(self.get_gfc(feature_collection))
            .reduceRegions(
                collection=feature_collection,
                reducer=ee.Reducer.sum().group(1),
                scale=ee.Image(param.gfc_dataset).projection().nominalScale(),
            )
        ).getInfo()

    @staticmethod
    def get_dataframe(result):
        """parse reduce region result as Pandas Dataframe

        Args:
            result (list): reduce region dictionary
        """

        hybas_stats = {}
        for feature in result["features"]:

            hybas_id = feature["properties"]["HYBAS_ID"]

            groups = feature["properties"]["groups"]
            zonal_stats = {group["group"]: group["sum"] for group in groups}

            hybas_stats[hybas_id] = zonal_stats

        df = (
            pd.melt(
                pd.DataFrame.from_dict(hybas_stats, "index")
                # .rename(columns=param.gfc_names)
                ,
                ignore_index=False,
            )
            .reset_index()
            .rename(columns={"index": "basin", "value": "area"})
        )

        # Prepare base dataframe
        df["basin"] = df.basin.astype(str)
        df["variable"] = df.variable.astype(int)
        df["group"] = df["variable"].apply(lambda x: cp.gfc_translation[x])

        # Create a year label and set 0 to everything is not forest-loss
        df["year"] = (
            df["variable"].apply(lambda x: x + 2000 if x <= 20 else 0).astype(int)
        )

        # Add a color for every catchment
        color_palette = np.array(
            sns.color_palette("hls", len(df.basin.unique())).as_hex()
        )

        random.shuffle(color_palette)

        df["catch_color"] = color_palette[pd.factorize(df.basin)[0]]

        return df

    def get_overall_pie_df(
        self,
    ):
        """Create a grouped dataframe to display overall pie statistics"""

        grouped_df = self.zonal_df.groupby(["group"]).sum().reset_index()
        grouped_df["color"] = grouped_df["group"].apply(
            lambda x: param.gfc_colors_dict[x]
        )

        return grouped_df

    def get_bar_df(self):

        catch_area_df = self.base_df.groupby(["basin"]).sum().reset_index()
