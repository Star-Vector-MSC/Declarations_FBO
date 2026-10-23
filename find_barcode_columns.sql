-- Read-only: list barcode-related fields, without reading any goods or credentials.
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('wb_supplies_goods', 'article')
  AND (column_name ILIKE '%barcode%'
       OR column_name ILIKE '%bar_code%'
       OR column_name ILIKE '%sku%')
ORDER BY table_schema, table_name, ordinal_position;
