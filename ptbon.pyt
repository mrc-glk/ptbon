# -*- #################
"""
Tool name: Point Bonitation
Source: boni.py
Author: mrc


The MIT License

Copyright (c) 2018 mrc

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import arcpy
import argparse
import datetime
import os


def log(msg):
    if arc_env:
        arcpy.AddMessage(msg)
    else:
        print msg

        
def create_geodb():
    now = datetime.datetime.now()
    gdbname = 'ptbon_' + now.strftime('%Y%m%d_%H%M%S') + '.gdb'

    gdbdir = os.environ['TMP']

    arcpy.CreateFileGDB_management(gdbdir, gdbname)
    return os.path.join(gdbdir, gdbname)
    

class ClcLevel:
    def __init__(self, name, codes, weight=None, is_selected=False):
        self.name = name
        self.codes = codes
        self.weight = weight
        self.is_selected = is_selected


clc_levels = {
    'UrbanFabric' : ClcLevel('UrbanFabric', ['111', '112'], -20, True),
    'IndustrialCommercial' : ClcLevel('IndustrialCommercial', ['121', '122', '123', '124'], -30, True),
    'ArableLand' : ClcLevel('ArableLand', ['211', '212', '213'], 15, True),
    'PermanentCrops' : ClcLevel('PermanentCrops', ['221', '222', '223'], 10, False),
    'Pastures' : ClcLevel('Pastures', ['231'], 20, True),
    'HeterogeneousAgricultural' : ClcLevel('HeterogeneousAgricultural', ['241', '242', '243', '244'], 15, False),
    'Forests': ClcLevel('Forests', ['311', '312', '313'], 25, True),
    'ScrubsHerbaVege' : ClcLevel('ScrubsHerbaVege', ['321', '322', '323', '324'], 20, False),
    'OpenSpacesNoVege' : ClcLevel('OpenSpacesNoVege', ['331', '332', '333', '334', '335'], 10, False),
    'InlandWetlands' : ClcLevel('InlandWetlands', ['411', '412'], 20, False),
    'MaritimeWetlands' : ClcLevel('MaritimeWetlands', ['421', '422', '423'], 20, False),
    'InlandWaters' : ClcLevel('InlandWaters', ['511', '512'], 15, True),
    'MaritimeWaters' : ClcLevel('MaritimeWaters', ['512', '522', '523'], 15, False),
    # TODO add more CLC levels
}

    
class ClcLayer:
    def __init__(self, gdb, clc_level, weight, clc_version_field):
        self.level = clc_level
        self.name = clc_level.name
        self.gdb = gdb
        self.feature = os.path.join(gdb, 'clc' + self.name)
        self.joint_feature = os.path.join(gdb, 'joint' + self.name)
        self.area_field = 'area_' + self.name
        self.coverage_field = 'coverage_' + self.name
        self.weight = weight
        self.clc_version_field = clc_version_field

    def __get_query(self):
        return "\"{}\" IN ( '{}' )".format(self.clc_version_field, "','".join(self.level.codes))

    # extract particular CLC to separate layer
    def create_layer(self, grid_feature, clc_feature):
        tmp_layer = os.path.join(self.gdb, 'tmp' + self.name)
        arcpy.MakeFeatureLayer_management(clc_feature, tmp_layer, self.__get_query(), "", "")
        arcpy.Identity_analysis(tmp_layer, grid_feature, self.feature, "ALL", "", "NO_RELATIONSHIPS")
        log('ClcLayer({})::create_layer() done'.format(self.name))

    # calculate area of CLC within grid cell
    def calculate_area(self):
        arcpy.AddField_management(self.feature, self.area_field, 'DOUBLE')

        area_expression = '!SHAPE.AREA@SQUAREKILOMETERS!'
        arcpy.CalculateField_management(self.feature, self.area_field, area_expression, 'PYTHON_9.3')
        log('ClcLayer({})::calculate_area() done'.format(self.name))

    # calculate % coverage (layer must be already joined with grid so we have cell area)
    def calculate_coverage(self):
        arcpy.AddField_management(self.joint_feature, self.coverage_field, 'DOUBLE')
        rows = arcpy.da.UpdateCursor(self.joint_feature, [self.coverage_field, self.area_field, 'area_cell'])
        for row in rows:
            row[1] = row[1] if row[1] != None else 0
            row[0] = row[1] / row[2]
            rows.updateRow(row)

        log('ClcLayer({})::calculate_coverage() done'.format(self.name))

    # join spatially with grid
    def join_spatially_with_grid(self, grid_feature):
        # define field mapping
        field_mappings = arcpy.FieldMappings()
        field_mappings.addTable(grid_feature)
        field_mappings.addTable(self.feature)

        # set merge rule on area_field
        area_fld_idx = field_mappings.findFieldMapIndex(self.area_field)
        area_fld_map = field_mappings.getFieldMap(area_fld_idx)
        area_fld_map.mergeRule = "sum"
        field_mappings.replaceFieldMap(area_fld_idx, area_fld_map)

        # finally do a spatial join
        arcpy.SpatialJoin_analysis(grid_feature, self.feature, self.joint_feature, "JOIN_ONE_TO_ONE", "KEEP_ALL", field_mappings, match_option="CONTAINS")
        log('ClcLayer({})::join_spatially_with_grid() done'.format(self.name))

    # TODO points should be calculated per layer and then passed to target grid instead of passing formula and/or python codeblock
    def get_formula(self):
        return '({} * {})'.format(self.level.weight, self.level.name)


class TargetGrid:
    def __init__(self, gdb):
        self.gdb = gdb
        self.clc_layers = []
        self.feature = os.path.join(gdb, 'grid')
        self.area_field = 'area_cell'
        self.points_field = 'points'

    def add_clc_layer(self, clc_layer):
        self.clc_layers.append(clc_layer)

    def make(self, input_shape):
        # build grid
        tmp_grid = os.path.join(self.gdb, 'tmp_grid')
        arcpy.GridIndexFeatures_cartography(tmp_grid, input_shape, "", "", "", "1000 Meters", "1000 Meters")

        # intersect with input shape
        input_features = u'{0} #;{1} #'.format(input_shape, tmp_grid)
        arcpy.Intersect_analysis(input_features, self.feature, "ALL", "", "INPUT")

        # remove spare fields
        required = ('FID', 'OBJECTID', 'Shape', 'Shape_Length', 'Shape_Area')
        spare_fields = [f.name for f in arcpy.ListFields(self.feature) if not f.name in required]
        arcpy.DeleteField_management(self.feature, spare_fields)

        log('TargetGrid::make() done')

    def calculate_area(self):
        arcpy.AddField_management(self.feature, self.area_field, 'DOUBLE')
        arcpy.CalculateField_management(self.feature, self.area_field, '!SHAPE.AREA@SQUAREKILOMETERS!', 'PYTHON_9.3')
        log('TargetGrid::calculate_area() done')

    def copy_clc_coverage(self):
        for clc_lyr in self.clc_layers:
            arcpy.JoinField_management(self.feature, 'OBJECTID', clc_lyr.joint_feature, 'TARGET_FID', clc_lyr.coverage_field)
        log('TargetGrid::copy_clc_coverage() done')

    def __build_ptbon_formula(self):
        form = ''
        for clc_lyr in self.clc_layers:
            form += clc_lyr.get_formula() + ' + '
        form += '0\n'
        return form

    def __build_ptbon_expression(self):
        expr = 'calc_ptbon('
        for clc_lyr in self.clc_layers:
            expr += '!{}!, '.format(clc_lyr.coverage_field)
        expr += 'None)\n'
        return expr

    def __build_ptbon_codeblock(self):
        codeblock = 'def calc_ptbon('
        for clc_lyr in self.clc_layers:
            codeblock += '{}, '.format(clc_lyr.level.name)
        codeblock += 'empty=None):\n'
        codeblock += '    return int({})\n'.format(self.__build_ptbon_formula())
        return codeblock

    def assign_points(self):
        arcpy.AddField_management(self.feature, self.points_field, 'INTEGER')

        # this is a way too hacky and should be replaced with update cursor
        ptbon_expr = self.__build_ptbon_expression()
        ptbon_codeblock = self.__build_ptbon_codeblock()
        arcpy.CalculateField_management(self.feature, self.points_field, ptbon_expr, 'PYTHON_9.3', ptbon_codeblock)

        log('TargetGrid::assign_points() done')
        
    def copy_features_to_target(self, tgt_feature):
        arcpy.CopyFeatures_management(self.feature, tgt_feature)
        log('TargetGrid::copy_features_to_target() done')

        
class SourceClc:
    def __init__(self, gdb):
        self.gdb = gdb
        self.feature = os.path.join(gdb, 'clc')
        self.clc_version_field = 'code_12'

    def __determine_version(self):
        # self.clc_version_field = find 'code_XX' field in self.feature attributes
        # TO BE DONE LATER
        pass

    def cut(self, clc_feature, input_shape):
        input_features = u'{0} #;{1} #'.format(input_shape, clc_feature)
        arcpy.Intersect_analysis(input_features, self.feature, "ALL", "", "INPUT")

        self.__determine_version()

        required = (self.clc_version_field, 'FID', 'OBJECTID', 'Shape', 'Shape_Length', 'Shape_Area')
        spare_fields = [f.name for f in arcpy.ListFields(self.feature) if f.name not in required]
        arcpy.DeleteField_management(self.feature, spare_fields)
        log('SourceClc::cut() done')


def ptbon(input_region, input_clc, corine_layers, out_feature):

    gdb = create_geodb()

    tgt_grid = TargetGrid(gdb)
    tgt_grid.make(input_region)
    tgt_grid.calculate_area()

    src_clc = SourceClc(gdb)
    src_clc.cut(input_clc, input_region)
            
    for lvl_name,weight in corine_layers:
        clc_layer = ClcLayer(gdb, clc_levels[lvl_name], weight, src_clc.clc_version_field)

        clc_layer.create_layer(tgt_grid.feature, src_clc.feature)
        clc_layer.calculate_area()
        clc_layer.join_spatially_with_grid(tgt_grid.feature)
        clc_layer.calculate_coverage()

        tgt_grid.add_clc_layer(clc_layer)

    tgt_grid.copy_clc_coverage()
    tgt_grid.assign_points()
    tgt_grid.copy_features_to_target(out_feature)

    
class Toolbox(object):
    def __init__(self):
        self.label = 'Point Bonitation Tools'
        self.alias = 'pointbonitation'

        # List of tool classes associated with this toolbox
        self.tools = [PointBonitation]


class PointBonitation(object):
    def __init__(self):
        self.label = 'Calculate'
        self.description = 'Measures landscape attractiveness for region' + \
                           'based on Corine Land Cover data.'

    def getParameterInfo(self):
        # Define parameter definitions

        # Input Feature parameter
        in_feature = arcpy.Parameter(
            displayName='Input feature',
            name='in_features',
            datatype='GPFeatureLayer',
            parameterType='Required',
            direction='Input')

        in_feature.filter.list = ['Polygon']

        # CLC Feature parameter
        clc_feature = arcpy.Parameter(
            displayName='Corine Land Cover features',
            name='clc_feature',
            datatype='GPFeatureLayer',
            parameterType='Required',
            direction='Input')

        clc_feature.filter.list = ['Polygon']

        corine_levels = arcpy.Parameter(
            displayName='Corine levels',
            name='corine_levels',
            datatype='GPValueTable',
            parameterType='Required',
            multiValue=True,
            direction='Input')

        corine_levels.columns = [['GPString', 'Level 2'], ['GPDouble', 'Points'], ['GPBoolean', 'Selected']]
        corine_levels.filters[2].type = 'ValueList'
        corine_levels.filters[2].list = ['true', 'false']
        
        clist = []
        for lvl in clc_levels.values():
            is_selected = str(lvl.is_selected).lower()
            clist.append([lvl.name, lvl.weight, is_selected])
        corine_levels.values = clist
        
        # Derived Output Features parameter
        out_feature = arcpy.Parameter(
            displayName='Output feature',
            name='out_feature',
            datatype='GPFeatureLayer',
            parameterType='Required',
            direction='Output')

        out_feature.parameterDependencies = [in_feature.name, clc_feature.name]
        out_feature.schema.clone = True

        parameters = [in_feature, clc_feature, corine_levels, out_feature]

        return parameters

    def isLicensed(self):  # optional
        return True

    def updateParameters(self, parameters):  # optional
        if parameters[2].altered:
            arcpy.AddMessage('altered')
        return

    def updateMessages(self, parameters):  # optional
        return

    def execute(self, parameters, messages):
        arc_env = True
        
        try:            
            input_feature = parameters[0].valueAsText
            clc_feature = parameters[1].valueAsText
            out_feature = parameters[3].valueAsText
 
            corine_levels = [(val[0], val[1]) for val in parameters[2].value if val[2] != False]
                        
            ptbon(input_feature, clc_feature, corine_levels, out_feature)
            
        except arcpy.ExecuteError:
            messages.addErrorMessage(arcpy.GetMessages())  


if __name__ == '__main__':
    arc_env = False
    
    parser = argparse.ArgumentParser(description='Measures landscape attractiveness for region' + \
                           'based on Corine Land Cover data.')
    # to be done
