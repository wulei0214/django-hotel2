import pymysql

# Django 4.1 要求 mysqlclient ≥ 1.4.3；PyMySQL 1.0.2 官方兼容做法：
# 声明兼容版本号后再伪装（否则间歇性 ImproperlyConfigured: you have 1.0.2）
pymysql.version_info = (1, 4, 6, "final", 0)
pymysql.install_as_MySQLdb()
