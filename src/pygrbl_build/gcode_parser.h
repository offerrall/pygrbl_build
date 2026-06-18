#ifndef GCODE_PARSER_H
#define GCODE_PARSER_H

#define GCODE_SUCCESS 0
#define GCODE_ERROR_FILE_NOT_FOUND -1
#define GCODE_ERROR_NO_COORDINATES -2

void update_bbox_from_line(const char* line,
                           double* min_x,
                           double* max_x,
                           double* min_y,
                           double* max_y);

int get_bounding_box(const char* file_path,
                     double* min_x,
                     double* max_x,
                     double* min_y,
                     double* max_y);

int get_bounding_box_buffer(const char* data,
                            long len,
                            double* min_x,
                            double* max_x,
                            double* min_y,
                            double* max_y);

#endif
