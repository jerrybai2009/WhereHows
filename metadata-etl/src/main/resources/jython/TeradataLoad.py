#
# Copyright 2015 LinkedIn Corp. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#

import sys
from com.ziclix.python.sql import zxJDBC
from wherehows.common import Constant
from org.slf4j import LoggerFactory


class TeradataLoad:
  def __init__(self):
    self.logger = LoggerFactory.getLogger('jython script : ' + self.__class__.__name__)

  def load_metadata(self):
    cursor = self.conn_mysql.cursor()
    load_cmd = '''
        DELETE FROM stg_dict_dataset WHERE db_id = {db_id};

        LOAD DATA LOCAL INFILE '{source_file}'
        INTO TABLE stg_dict_dataset
        FIELDS TERMINATED BY '\Z' ESCAPED BY '\0'
        (`name`, `schema`, properties, fields, urn, source, sample_partition_full_path, source_created_time, source_modified_time)
        SET db_id = {db_id},
        storage_type = 'Table',
        wh_etl_exec_id = {wh_etl_exec_id};

        -- SELECT COUNT(*) FROM stg_dict_dataset;
        -- clear
        DELETE FROM stg_dict_dataset where db_id = {db_id}
          AND (length(`name`) = 0
           OR `name` like 'tmp\_%'
           OR `name` like '%\_tmp'
           OR `name` like '#%'
           OR `name` like 'ut\_%'
           OR `name` regexp '^LS_[[:alnum:]_]+_[[:digit:]]')
        ;

        update stg_dict_dataset
        set location_prefix = substring_index(substring_index(urn, '/', 4), '/', -2) /* teradata location_prefix is it's schema name*/
        WHERE db_id = {db_id} and location_prefix is null;

        update stg_dict_dataset
        set parent_name = substring_index(substring_index(urn, '/', 4), '/', -1) /* teradata parent_name is it's schema name*/
        where db_id = {db_id} and parent_name is null;

        -- insert into final table
        INSERT INTO dict_dataset
        ( `name`,
          `schema`,
          schema_type,
          fields,
          properties,
          urn,
          source,
          location_prefix,
          parent_name,
          storage_type,
          ref_dataset_id,
          status_id,
          dataset_type,
          hive_serdes_class,
          is_partitioned,
          partition_layout_pattern_id,
          sample_partition_full_path,
          source_created_time,
          source_modified_time,
          created_time,
          wh_etl_exec_id
        )
        select s.name, s.schema, s.schema_type, s.fields,
          s.properties, s.urn,
          s.source, s.location_prefix, s.parent_name,
          s.storage_type, s.ref_dataset_id, s.status_id,
          s.dataset_type, s.hive_serdes_class, s.is_partitioned,
          s.partition_layout_pattern_id, s.sample_partition_full_path,
          s.source_created_time, s.source_modified_time, UNIX_TIMESTAMP(now()),
          s.wh_etl_exec_id
        from stg_dict_dataset s
        where s.db_id = {db_id}
        on duplicate key update
          `name`=s.name, `schema`=s.schema, schema_type=s.schema_type, fields=s.fields,
          properties=s.properties, source=s.source, location_prefix=s.location_prefix, parent_name=s.parent_name,
            storage_type=s.storage_type, ref_dataset_id=s.ref_dataset_id, status_id=s.status_id,
                     dataset_type=s.dataset_type, hive_serdes_class=s.hive_serdes_class, is_partitioned=s.is_partitioned,
          partition_layout_pattern_id=s.partition_layout_pattern_id, sample_partition_full_path=s.sample_partition_full_path,
          source_created_time=s.source_created_time, source_modified_time=s.source_modified_time,
            modified_time=UNIX_TIMESTAMP(now()), wh_etl_exec_id=s.wh_etl_exec_id
        ;
        analyze table dict_dataset;
        '''.format(source_file=self.input_file, db_id=self.db_id, wh_etl_exec_id=self.wh_etl_exec_id)

    for state in load_cmd.split(";"):
      self.logger.debug(state)
      cursor.execute(state)
      self.conn_mysql.commit()
    cursor.close()

  def load_field(self):
    cursor = self.conn_mysql.cursor()
    load_cmd = '''
        DELETE FROM stg_dict_field_detail where db_id = {db_id};

        LOAD DATA LOCAL INFILE '{source_file}'
        INTO TABLE stg_dict_field_detail
        FIELDS TERMINATED BY '\Z'
        (urn, sort_id, parent_sort_id, parent_path, field_name, field_label, data_type,
         data_size, @precision, @scale, is_nullable,
         is_indexed, is_partitioned, default_value, namespace, description,
         @dummy
        )
        set
          data_precision=nullif(@precision,'')
        , data_scale=nullif(@scale,'')
        , db_id = {db_id}
        ;

        analyze table stg_dict_field_detail;

        update stg_dict_field_detail
        set description = null
        where db_id = {db_id}
           and (char_length(trim(description)) = 0
           or description in ('null', 'N/A', 'nothing', 'empty', 'none'));

        -- delete old record if it does not exist in this load batch anymore (but have the dataset id)
        create temporary table if not exists t_deleted_fields (primary key (field_id))
          select x.field_id
            from stg_dict_field_detail s
              join dict_dataset i
                on s.urn = i.urn
                and s.db_id = {db_id}
              right join dict_field_detail x
                on i.id = x.dataset_id
                and s.field_name = x.field_name
                and s.parent_path = x.parent_path
          where s.field_name is null
            and x.dataset_id in (
                       select d.id dataset_id
                       from stg_dict_field_detail k join dict_dataset d
                         on k.urn = d.urn
                        and k.db_id = {db_id}
            )
        ; -- run time : ~2min

        delete from dict_field_detail where field_id in (select field_id from t_deleted_fields);
    
        -- update the old record if some thing changed
        update dict_field_detail t join
        (
          select x.field_id, s.*
          from stg_dict_field_detail s join dict_dataset d
            on s.urn = d.urn
               join dict_field_detail x
           on s.field_name = x.field_name
          and coalesce(s.parent_path, '*') = coalesce(x.parent_path, '*')
          and d.id = x.dataset_id
          where s.db_id = {db_id}
            and (x.sort_id <> s.sort_id
                or x.parent_sort_id <> s.parent_sort_id
                or x.data_type <> s.data_type
                or x.data_size <> s.data_size or (x.data_size is null XOR s.data_size is null)
                or x.data_precision <> s.data_precision or (x.data_precision is null XOR s.data_precision is null)
                or x.is_nullable <> s.is_nullable or (x.is_nullable is null XOR s.is_nullable is null)
                or x.is_partitioned <> s.is_partitioned or (x.is_partitioned is null XOR s.is_partitioned is null)
                or x.is_distributed <> s.is_distributed or (x.is_distributed is null XOR s.is_distributed is null)
                or x.default_value <> s.default_value or (x.default_value is null XOR s.default_value is null)
                or x.namespace <> s.namespace or (x.namespace is null XOR s.namespace is null)
            )
        ) p
          on t.field_id = p.field_id
        set t.sort_id = p.sort_id,
            t.parent_sort_id = p.parent_sort_id,
            t.data_type = p.data_type,
            t.data_size = p.data_size,
            t.data_precision = p.data_precision,
            t.is_nullable = p.is_nullable,
            t.is_partitioned = p.is_partitioned,
            t.is_distributed = p.is_distributed,
            t.default_value = p.default_value,
            t.namespace = p.namespace,
            t.modified = now()
        ;

        show warnings limit 10;

        insert into dict_field_detail (
        dataset_id, fields_layout_id, sort_id, parent_sort_id,
        field_name, data_type, data_size, data_precision, data_fraction,
        is_nullable, /* is_indexed, is_partitioned, is_distributed, */
        modified
        )
        select
          d.id,
          0 as fields_layout_id,
          s.sort_id,
          0 parent_sort_id,
          s.field_name,
          s.data_type,
          s.data_size,
          s.data_precision,
          s.data_scale,
          s.is_nullable,
          now()
        from stg_dict_field_detail s join dict_dataset d
          on s.urn = d.urn
             left join dict_field_detail f
          on d.id = f.dataset_id
         and s.field_name = f.field_name
        where db_id = {db_id} and f.field_id is null;

        analyze table dict_field_detail;
        '''.format(source_file=self.input_field_file, db_id=self.db_id)

    for state in load_cmd.split(";"):
      self.logger.debug(state)
      cursor.execute(state)
      self.conn_mysql.commit()
    cursor.close()

  def load_sample(self):
    load_cmd = '''
    DELETE FROM stg_dict_dataset_sample WHERE db_id = {db_id};

    LOAD DATA LOCAL INFILE '{source_file}'
    INTO TABLE stg_dict_dataset_sample FIELDS TERMINATED BY '\Z' ESCAPED BY '\0'
    (urn,ref_urn,data)
    SET db_id = {db_id};

    -- update reference id in stagging table
    UPDATE  stg_dict_dataset_sample s
    LEFT JOIN dict_dataset d ON s.ref_urn = d.urn
    SET s.ref_id = d.id
    WHERE s.db_id = {db_id};

    -- first insert ref_id as 0
    INSERT INTO dict_dataset_sample
    ( `dataset_id`,
      `urn`,
      `ref_id`,
      `data`,
      created
    )
    select d.id as dataset_id, s.urn, s.ref_id, s.data, now()
    from stg_dict_dataset_sample s left join dict_dataset d on d.urn = s.urn
          where s.db_id = {db_id}
    on duplicate key update
      `data`=s.data, modified=now();


    -- update reference id in final table
    UPDATE dict_dataset_sample d
    RIGHT JOIN stg_dict_dataset_sample s ON d.urn = s.urn
    SET d.ref_id = s.ref_id
    WHERE s.db_id = {db_id} AND d.ref_id = 0;

    '''.format(source_file=self.input_sampledata_file, db_id=self.db_id)

    cursor = self.conn_mysql.cursor()
    for state in load_cmd.split(";"):
      self.logger.debug(state)
      cursor.execute(state)
      self.conn_mysql.commit()
    cursor.close()


if __name__ == "__main__":
  args = sys.argv[1]

  l = TeradataLoad()

  # set up connection
  username = args[Constant.WH_DB_USERNAME_KEY]
  password = args[Constant.WH_DB_PASSWORD_KEY]
  JDBC_DRIVER = args[Constant.WH_DB_DRIVER_KEY]
  JDBC_URL = args[Constant.WH_DB_URL_KEY]

  l.input_file = args[Constant.TD_METADATA_KEY]
  l.input_field_file = args[Constant.TD_FIELD_METADATA_KEY]
  l.input_sampledata_file = args[Constant.TD_SAMPLE_OUTPUT_KEY]
  l.db_id = args[Constant.DB_ID_KEY]
  l.wh_etl_exec_id = args[Constant.WH_EXEC_ID_KEY]
  l.conn_mysql = zxJDBC.connect(JDBC_URL, username, password, JDBC_DRIVER)
  try:
    l.load_metadata()
    l.load_field()
    l.load_sample()
  finally:
    l.conn_mysql.close()
