-- MySQL dump 10.13  Distrib 8.0.33, for Win64 (x86_64)
--
-- Host: localhost    Database: hotel
-- ------------------------------------------------------
-- Server version	8.0.33

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `accommodation`
--

DROP TABLE IF EXISTS `accommodation`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `accommodation` (
  `入住时间` varchar(45) DEFAULT NULL,
  `id` int NOT NULL AUTO_INCREMENT,
  `客房编号` int NOT NULL,
  `顾客id` int NOT NULL,
  `冲账人` varchar(45) DEFAULT NULL,
  `冲账时间` varchar(45) DEFAULT NULL,
  `收款金额` varchar(45) DEFAULT NULL,
  `状态` varchar(10) DEFAULT NULL,
  `联系电话` varchar(45) DEFAULT NULL,
  `退房时间` varchar(45) DEFAULT NULL,
  `押金` varchar(45) DEFAULT NULL,
  `入住日期` date DEFAULT NULL,
  `实际入住时间` datetime(6) DEFAULT NULL,
  `实际退房时间` datetime(6) DEFAULT NULL,
  `离店日期` date DEFAULT NULL,
  `reservation_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `accommodation_客房编号_eb67e2dc_fk_room_房间号` (`客房编号`),
  KEY `accommodation_顾客id_62abfda5_fk_client_顾客id` (`顾客id`),
  KEY `accommodation_reservation_id_345168e3_fk_reservation_id` (`reservation_id`),
  KEY `accommodation_入住日期_8cb0e9e1` (`入住日期`),
  KEY `accommodation_离店日期_4fe48ab5` (`离店日期`),
  CONSTRAINT `accommodation_reservation_id_345168e3_fk_reservation_id` FOREIGN KEY (`reservation_id`) REFERENCES `reservation` (`id`),
  CONSTRAINT `accommodation_客房编号_eb67e2dc_fk_room_房间号` FOREIGN KEY (`客房编号`) REFERENCES `room` (`房间号`),
  CONSTRAINT `accommodation_顾客id_62abfda5_fk_client_顾客id` FOREIGN KEY (`顾客id`) REFERENCES `client` (`顾客id`)
) ENGINE=InnoDB AUTO_INCREMENT=122 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `accommodation`
--

LOCK TABLES `accommodation` WRITE;
/*!40000 ALTER TABLE `accommodation` DISABLE KEYS */;
INSERT INTO `accommodation` VALUES ('2022-03-22',101,1,1,'manager','2026-10-30','188','已冲账',NULL,NULL,NULL,'2022-03-22',NULL,NULL,'2022-03-23',NULL),('2026-10-30',102,1,222111235,'manager','2026-10-30',NULL,'已冲账','13800001111',NULL,NULL,'2026-10-30',NULL,NULL,'2026-10-31',NULL),('2026-10-30',103,1,222111236,NULL,NULL,NULL,'待确认','17777777777',NULL,NULL,'2026-10-30',NULL,NULL,'2026-10-31',NULL),('2026-10-30',104,201,222111237,NULL,NULL,'268','已退房','13811112222','2026-10-31','500','2026-10-30',NULL,'2026-10-31 04:00:00.000000','2026-10-31',NULL),('2026-10-30',105,202,222111239,NULL,NULL,'268.00','已退房','13899990000','2026-10-31','300','2026-10-30',NULL,'2026-10-31 04:00:00.000000','2026-10-31',NULL),('2026-11-01',106,201,222111243,NULL,NULL,NULL,'已确认','13855556666',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',107,1,222111244,NULL,NULL,NULL,'待确认','13855556666',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-08',108,202,222111245,NULL,NULL,NULL,'待确认','13800009991',NULL,NULL,'2026-11-08',NULL,NULL,'2026-11-09',NULL),('2026-11-08',109,202,222111246,NULL,NULL,NULL,'待确认','13800009992',NULL,NULL,'2026-11-08',NULL,NULL,'2026-11-09',NULL),('2026-11-01',110,1,222111247,NULL,NULL,NULL,'待确认','13829791801',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',111,1,222111248,NULL,NULL,NULL,'待确认','13829799801',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',112,1,222111249,NULL,NULL,NULL,'待确认','13829807001',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',113,1,222111250,NULL,NULL,NULL,'待确认','13829818801',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',114,1,222111251,NULL,NULL,NULL,'待确认','13829829501',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',115,1,222111252,NULL,NULL,NULL,'待确认','13829831701',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-03',116,202,222111253,NULL,NULL,NULL,'已确认','13877770000',NULL,NULL,'2026-11-03',NULL,NULL,'2026-11-04',NULL),('2026-11-01',117,1,222111254,NULL,NULL,NULL,'待确认','13829836601',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',118,1,222111255,NULL,NULL,NULL,'待确认','13829838301',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',119,1,222111256,NULL,NULL,NULL,'待确认','13829840401',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',120,1,222111257,NULL,NULL,NULL,'待确认','13829843201',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL),('2026-11-01',121,1,222111258,NULL,NULL,NULL,'待确认','13829849401',NULL,NULL,'2026-11-01',NULL,NULL,'2026-11-02',NULL);
/*!40000 ALTER TABLE `accommodation` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `audit_log`
--

DROP TABLE IF EXISTS `audit_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `audit_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `action` varchar(50) NOT NULL,
  `entity` varchar(50) NOT NULL,
  `object_id` varchar(64) DEFAULT NULL,
  `before` json NOT NULL,
  `after` json NOT NULL,
  `ip` char(39) DEFAULT NULL,
  `created_at` datetime(6) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `audit_log`
--

LOCK TABLES `audit_log` WRITE;
/*!40000 ALTER TABLE `audit_log` DISABLE KEYS */;
/*!40000 ALTER TABLE `audit_log` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auth_group`
--

DROP TABLE IF EXISTS `auth_group`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_group` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(150) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auth_group`
--

LOCK TABLES `auth_group` WRITE;
/*!40000 ALTER TABLE `auth_group` DISABLE KEYS */;
INSERT INTO `auth_group` VALUES (2,'值班经理'),(1,'前台'),(4,'店长'),(3,'财务');
/*!40000 ALTER TABLE `auth_group` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auth_group_permissions`
--

DROP TABLE IF EXISTS `auth_group_permissions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_group_permissions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `group_id` int NOT NULL,
  `permission_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_group_permissions_group_id_permission_id_0cd325b0_uniq` (`group_id`,`permission_id`),
  KEY `auth_group_permissio_permission_id_84c5c92e_fk_auth_perm` (`permission_id`),
  CONSTRAINT `auth_group_permissio_permission_id_84c5c92e_fk_auth_perm` FOREIGN KEY (`permission_id`) REFERENCES `auth_permission` (`id`),
  CONSTRAINT `auth_group_permissions_group_id_b120cbf9_fk_auth_group_id` FOREIGN KEY (`group_id`) REFERENCES `auth_group` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=69 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auth_group_permissions`
--

LOCK TABLES `auth_group_permissions` WRITE;
/*!40000 ALTER TABLE `auth_group_permissions` DISABLE KEYS */;
INSERT INTO `auth_group_permissions` VALUES (11,1,25),(12,1,26),(10,1,28),(2,1,29),(3,1,30),(1,1,32),(16,1,36),(8,1,37),(9,1,38),(7,1,40),(14,1,41),(15,1,42),(13,1,44),(17,1,48),(5,1,53),(6,1,54),(4,1,56),(28,2,25),(29,2,26),(36,2,27),(27,2,28),(19,2,29),(20,2,30),(18,2,32),(33,2,36),(25,2,37),(26,2,38),(35,2,39),(24,2,40),(31,2,41),(32,2,42),(30,2,44),(34,2,46),(68,2,48),(22,2,53),(23,2,54),(21,2,56),(40,3,28),(37,3,32),(41,3,36),(39,3,40),(43,3,44),(42,3,48),(38,3,56),(66,4,25),(67,4,26),(65,4,28),(48,4,29),(49,4,30),(47,4,32),(51,4,33),(52,4,34),(50,4,36),(63,4,37),(64,4,38),(62,4,40),(57,4,41),(58,4,42),(56,4,44),(60,4,45),(61,4,46),(59,4,48),(45,4,49),(46,4,50),(44,4,52),(54,4,53),(55,4,54),(53,4,56);
/*!40000 ALTER TABLE `auth_group_permissions` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auth_permission`
--

DROP TABLE IF EXISTS `auth_permission`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_permission` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `content_type_id` int NOT NULL,
  `codename` varchar(100) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_permission_content_type_id_codename_01ab375a_uniq` (`content_type_id`,`codename`),
  CONSTRAINT `auth_permission_content_type_id_2f476e4b_fk_django_co` FOREIGN KEY (`content_type_id`) REFERENCES `django_content_type` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=89 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auth_permission`
--

LOCK TABLES `auth_permission` WRITE;
/*!40000 ALTER TABLE `auth_permission` DISABLE KEYS */;
INSERT INTO `auth_permission` VALUES (1,'Can add log entry',1,'add_logentry'),(2,'Can change log entry',1,'change_logentry'),(3,'Can delete log entry',1,'delete_logentry'),(4,'Can view log entry',1,'view_logentry'),(5,'Can add permission',2,'add_permission'),(6,'Can change permission',2,'change_permission'),(7,'Can delete permission',2,'delete_permission'),(8,'Can view permission',2,'view_permission'),(9,'Can add group',3,'add_group'),(10,'Can change group',3,'change_group'),(11,'Can delete group',3,'delete_group'),(12,'Can view group',3,'view_group'),(13,'Can add user',4,'add_user'),(14,'Can change user',4,'change_user'),(15,'Can delete user',4,'delete_user'),(16,'Can view user',4,'view_user'),(17,'Can add content type',5,'add_contenttype'),(18,'Can change content type',5,'change_contenttype'),(19,'Can delete content type',5,'delete_contenttype'),(20,'Can view content type',5,'view_contenttype'),(21,'Can add session',6,'add_session'),(22,'Can change session',6,'change_session'),(23,'Can delete session',6,'delete_session'),(24,'Can view session',6,'view_session'),(25,'Can add accommodation',7,'add_accommodation'),(26,'Can change accommodation',7,'change_accommodation'),(27,'Can delete accommodation',7,'delete_accommodation'),(28,'Can view accommodation',7,'view_accommodation'),(29,'Can add client',8,'add_client'),(30,'Can change client',8,'change_client'),(31,'Can delete client',8,'delete_client'),(32,'Can view client',8,'view_client'),(33,'Can add food',9,'add_food'),(34,'Can change food',9,'change_food'),(35,'Can delete food',9,'delete_food'),(36,'Can view food',9,'view_food'),(37,'Can add order',10,'add_order'),(38,'Can change order',10,'change_order'),(39,'Can delete order',10,'delete_order'),(40,'Can view order',10,'view_order'),(41,'Can add park',11,'add_park'),(42,'Can change park',11,'change_park'),(43,'Can delete park',11,'delete_park'),(44,'Can view park',11,'view_park'),(45,'Can add room',12,'add_room'),(46,'Can change room',12,'change_room'),(47,'Can delete room',12,'delete_room'),(48,'Can view room',12,'view_room'),(49,'Can add staff',13,'add_staff'),(50,'Can change staff',13,'change_staff'),(51,'Can delete staff',13,'delete_staff'),(52,'Can view staff',13,'view_staff'),(53,'Can add vip',14,'add_vip'),(54,'Can change vip',14,'change_vip'),(55,'Can delete vip',14,'delete_vip'),(56,'Can view vip',14,'view_vip'),(57,'Can add 财务流水',15,'add_financerecord'),(58,'Can change 财务流水',15,'change_financerecord'),(59,'Can delete 财务流水',15,'delete_financerecord'),(60,'Can view 财务流水',15,'view_financerecord'),(61,'Can add 夜审记录',16,'add_nightaudit'),(62,'Can change 夜审记录',16,'change_nightaudit'),(63,'Can delete 夜审记录',16,'delete_nightaudit'),(64,'Can view 夜审记录',16,'view_nightaudit'),(65,'Can add 账单流水',17,'add_folio'),(66,'Can change 账单流水',17,'change_folio'),(67,'Can delete 账单流水',17,'delete_folio'),(68,'Can view 账单流水',17,'view_folio'),(69,'Can add 审计日志',18,'add_auditlog'),(70,'Can change 审计日志',18,'change_auditlog'),(71,'Can delete 审计日志',18,'delete_auditlog'),(72,'Can view 审计日志',18,'view_auditlog'),(73,'Can add 统一账本',19,'add_ledgerentry'),(74,'Can change 统一账本',19,'change_ledgerentry'),(75,'Can delete 统一账本',19,'delete_ledgerentry'),(76,'Can view 统一账本',19,'view_ledgerentry'),(77,'Can add 支付流水',20,'add_payment'),(78,'Can change 支付流水',20,'change_payment'),(79,'Can delete 支付流水',20,'delete_payment'),(80,'Can view 支付流水',20,'view_payment'),(81,'Can add 预订',21,'add_reservation'),(82,'Can change 预订',21,'change_reservation'),(83,'Can delete 预订',21,'delete_reservation'),(84,'Can view 预订',21,'view_reservation'),(85,'Can add 房型日库存',22,'add_roomtypeinventory'),(86,'Can change 房型日库存',22,'change_roomtypeinventory'),(87,'Can delete 房型日库存',22,'delete_roomtypeinventory'),(88,'Can view 房型日库存',22,'view_roomtypeinventory');
/*!40000 ALTER TABLE `auth_permission` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auth_user`
--

DROP TABLE IF EXISTS `auth_user`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_user` (
  `id` int NOT NULL AUTO_INCREMENT,
  `password` varchar(128) NOT NULL,
  `last_login` datetime(6) DEFAULT NULL,
  `is_superuser` tinyint(1) NOT NULL,
  `username` varchar(150) NOT NULL,
  `first_name` varchar(150) NOT NULL,
  `last_name` varchar(150) NOT NULL,
  `email` varchar(254) NOT NULL,
  `is_staff` tinyint(1) NOT NULL,
  `is_active` tinyint(1) NOT NULL,
  `date_joined` datetime(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auth_user`
--

LOCK TABLES `auth_user` WRITE;
/*!40000 ALTER TABLE `auth_user` DISABLE KEYS */;
INSERT INTO `auth_user` VALUES (1,'pbkdf2_sha256$390000$yERQGEUfROHICtbgwZ3l3b$etb1MmBgPw0sPpvELXVl+thozcpF6AOdCDzbrr36KMA=','2026-10-30 14:53:52.978423',1,'admin','','','123@qq.com',1,1,'2022-03-22 05:47:09.487388'),(2,'pbkdf2_sha256$390000$paevQyBjaXTjyOZcMPaj0q$EhXBbdRlD5roeH+ndzkrmJXlm/rpFrPqo8I1QGuZTfs=','2026-10-29 18:30:25.832972',0,'front','','','',1,1,'2026-10-29 15:45:12.419215'),(3,'pbkdf2_sha256$390000$57XNIQUUoK0tzktKHLu2xX$mgMk+JgcpkKxGT47yAsdHo8pCjTyiAaEYdEZiG+NJrQ=','2026-10-29 18:28:15.368799',0,'manager','','','',1,1,'2026-10-29 15:45:12.582702'),(4,'pbkdf2_sha256$390000$kUfw5XAg0A4TncmR4P06aT$AFpw48KhGDRH3WlQjWAMeu3Es03PhQH1keU+vq38eNY=','2026-10-29 18:28:15.572735',0,'finance','','','',1,1,'2026-10-29 15:45:12.744919'),(5,'pbkdf2_sha256$390000$yM4Wv6xA2lEKoO5jJi2xyI$q0Qphb3A6Cn083kdhlb8l58wk+Yz/I11IUxdiE0rNhE=','2026-10-29 18:30:24.272632',0,'owner','','','',1,1,'2026-10-29 15:45:12.895909'),(6,'pbkdf2_sha256$390000$3H5ir4keydQo84gtQQdsqA$telvjmLPUbgHHnmCHJID9rf9r6v/b7wkIrOQ75iCzJE=',NULL,0,'test','w','w','1458524545@qq.com',0,1,'2026-10-29 15:54:00.000000'),(7,'pbkdf2_sha256$390000$Osmb3ducoxQqcLky3strSv$aOi6Lvt/3pOmptX/ju6rN3YycAVTeUOKSfwn505WV6g=','2026-10-29 16:17:47.904754',0,'testfront','测试前台','','',1,1,'2026-10-29 16:17:47.752862');
/*!40000 ALTER TABLE `auth_user` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auth_user_groups`
--

DROP TABLE IF EXISTS `auth_user_groups`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_user_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `group_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_user_groups_user_id_group_id_94350c0c_uniq` (`user_id`,`group_id`),
  KEY `auth_user_groups_group_id_97559544_fk_auth_group_id` (`group_id`),
  CONSTRAINT `auth_user_groups_group_id_97559544_fk_auth_group_id` FOREIGN KEY (`group_id`) REFERENCES `auth_group` (`id`),
  CONSTRAINT `auth_user_groups_user_id_6a12ed8b_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auth_user_groups`
--

LOCK TABLES `auth_user_groups` WRITE;
/*!40000 ALTER TABLE `auth_user_groups` DISABLE KEYS */;
INSERT INTO `auth_user_groups` VALUES (1,2,1),(2,3,2),(3,4,3),(4,5,4),(5,6,2),(6,7,1);
/*!40000 ALTER TABLE `auth_user_groups` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auth_user_user_permissions`
--

DROP TABLE IF EXISTS `auth_user_user_permissions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_user_user_permissions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `permission_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_user_user_permissions_user_id_permission_id_14a6b632_uniq` (`user_id`,`permission_id`),
  KEY `auth_user_user_permi_permission_id_1fbb5f2c_fk_auth_perm` (`permission_id`),
  CONSTRAINT `auth_user_user_permi_permission_id_1fbb5f2c_fk_auth_perm` FOREIGN KEY (`permission_id`) REFERENCES `auth_permission` (`id`),
  CONSTRAINT `auth_user_user_permissions_user_id_a95ead1b_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auth_user_user_permissions`
--

LOCK TABLES `auth_user_user_permissions` WRITE;
/*!40000 ALTER TABLE `auth_user_user_permissions` DISABLE KEYS */;
INSERT INTO `auth_user_user_permissions` VALUES (2,6,22),(1,6,38);
/*!40000 ALTER TABLE `auth_user_user_permissions` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `billing_folio`
--

DROP TABLE IF EXISTS `billing_folio`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `billing_folio` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `status` varchar(12) NOT NULL,
  `currency` varchar(3) NOT NULL,
  `opened_at` datetime(6) NOT NULL,
  `closed_at` datetime(6) DEFAULT NULL,
  `guest_id` bigint NOT NULL,
  `property_id` bigint NOT NULL,
  `reservation_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `reservation_id` (`reservation_id`),
  KEY `billing_folio_guest_id_1b85a603_fk_guests_guest_id` (`guest_id`),
  KEY `billing_folio_property_id_bfbbf9e4_fk_core_property_id` (`property_id`),
  CONSTRAINT `billing_folio_guest_id_1b85a603_fk_guests_guest_id` FOREIGN KEY (`guest_id`) REFERENCES `guests_guest` (`id`),
  CONSTRAINT `billing_folio_property_id_bfbbf9e4_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `billing_folio_reservation_id_248cc417_fk_reservati` FOREIGN KEY (`reservation_id`) REFERENCES `reservations_reservation` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `billing_folio`
--

LOCK TABLES `billing_folio` WRITE;
/*!40000 ALTER TABLE `billing_folio` DISABLE KEYS */;
/*!40000 ALTER TABLE `billing_folio` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `billing_ledgerentry`
--

DROP TABLE IF EXISTS `billing_ledgerentry`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `billing_ledgerentry` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `entry_type` varchar(20) NOT NULL,
  `amount` decimal(12,2) NOT NULL,
  `currency` varchar(3) NOT NULL,
  `business_date` date NOT NULL,
  `reference` varchar(160) NOT NULL,
  `description` varchar(240) NOT NULL,
  `void_reason` varchar(240) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `folio_id` bigint NOT NULL,
  `posted_by_id` int DEFAULT NULL,
  `property_id` bigint NOT NULL,
  `stay_id` bigint DEFAULT NULL,
  `void_of_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `reference` (`reference`),
  KEY `idx_ledger_prop_date` (`property_id`,`business_date`),
  KEY `idx_ledger_folio_date` (`folio_id`,`business_date`),
  KEY `billing_ledgerentry_posted_by_id_3597090d_fk_auth_user_id` (`posted_by_id`),
  KEY `billing_ledgerentry_stay_id_794d62bd_fk_reservations_stay_id` (`stay_id`),
  KEY `billing_ledgerentry_void_of_id_72baba63_fk_billing_l` (`void_of_id`),
  CONSTRAINT `billing_ledgerentry_folio_id_22ca3a24_fk_billing_folio_id` FOREIGN KEY (`folio_id`) REFERENCES `billing_folio` (`id`),
  CONSTRAINT `billing_ledgerentry_posted_by_id_3597090d_fk_auth_user_id` FOREIGN KEY (`posted_by_id`) REFERENCES `auth_user` (`id`),
  CONSTRAINT `billing_ledgerentry_property_id_83efc321_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `billing_ledgerentry_stay_id_794d62bd_fk_reservations_stay_id` FOREIGN KEY (`stay_id`) REFERENCES `reservations_stay` (`id`),
  CONSTRAINT `billing_ledgerentry_void_of_id_72baba63_fk_billing_l` FOREIGN KEY (`void_of_id`) REFERENCES `billing_ledgerentry` (`id`),
  CONSTRAINT `ck_ledger_amount_nonzero` CHECK ((`amount` <> 0)),
  CONSTRAINT `ck_ledger_reversal_has_reason` CHECK (((`void_of_id` is null) or (`void_reason` > _utf8mb4'')))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `billing_ledgerentry`
--

LOCK TABLES `billing_ledgerentry` WRITE;
/*!40000 ALTER TABLE `billing_ledgerentry` DISABLE KEYS */;
/*!40000 ALTER TABLE `billing_ledgerentry` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `billing_payment`
--

DROP TABLE IF EXISTS `billing_payment`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `billing_payment` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `provider` varchar(24) NOT NULL,
  `provider_ref` varchar(80) NOT NULL,
  `method` varchar(24) NOT NULL,
  `amount` decimal(12,2) NOT NULL,
  `currency` varchar(3) NOT NULL,
  `status` varchar(24) NOT NULL,
  `paid_at` datetime(6) DEFAULT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `folio_id` bigint NOT NULL,
  `property_id` bigint NOT NULL,
  `reservation_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `provider_ref` (`provider_ref`),
  KEY `billing_payment_folio_id_5d78d138_fk_billing_folio_id` (`folio_id`),
  KEY `billing_payment_property_id_272fe232_fk_core_property_id` (`property_id`),
  KEY `billing_payment_reservation_id_d2879b08_fk_reservati` (`reservation_id`),
  CONSTRAINT `billing_payment_folio_id_5d78d138_fk_billing_folio_id` FOREIGN KEY (`folio_id`) REFERENCES `billing_folio` (`id`),
  CONSTRAINT `billing_payment_property_id_272fe232_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `billing_payment_reservation_id_d2879b08_fk_reservati` FOREIGN KEY (`reservation_id`) REFERENCES `reservations_reservation` (`id`),
  CONSTRAINT `ck_payment_amount_positive` CHECK ((`amount` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `billing_payment`
--

LOCK TABLES `billing_payment` WRITE;
/*!40000 ALTER TABLE `billing_payment` DISABLE KEYS */;
/*!40000 ALTER TABLE `billing_payment` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `client`
--

DROP TABLE IF EXISTS `client`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `client` (
  `顾客id` int NOT NULL AUTO_INCREMENT,
  `姓名` varchar(45) DEFAULT NULL,
  `身份证号` varchar(45) DEFAULT NULL,
  `手机号` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`顾客id`),
  UNIQUE KEY `client_身份证号_901daf3d_uniq` (`身份证号`),
  UNIQUE KEY `手机号` (`手机号`)
) ENGINE=InnoDB AUTO_INCREMENT=222111261 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `client`
--

LOCK TABLES `client` WRITE;
/*!40000 ALTER TABLE `client` DISABLE KEYS */;
INSERT INTO `client` VALUES (1,'赵四','231012341234567890',NULL),(2,'克拉克森','230102199203141234',NULL),(222111,'李四','230103199507206789',NULL),(2221112,'李四','231012341234567891',NULL),(222111233,'李四','231232132432454322',NULL),(222111234,'测试','110101199003079999',NULL),(222111235,'张三丰','110101198803074221',NULL),(222111236,'莉莎','430522022545512415',NULL),(222111237,'希尔顿','110101199911119999',NULL),(222111239,'散客王','110101200001012345',NULL),(222111240,'周杰','110101198505051111',NULL),(222111241,'王菲','110101198808082222',NULL),(222111242,'回归顾客','110101199912340001',NULL),(222111243,'在线预订客','110101199912345678',NULL),(222111244,'在线预订客',NULL,NULL),(222111245,'并发预订甲',NULL,NULL),(222111246,'并发预订乙',NULL,NULL),(222111247,'在线预订客',NULL,NULL),(222111248,'在线预订客',NULL,NULL),(222111249,'在线预订客',NULL,NULL),(222111250,'在线预订客',NULL,NULL),(222111251,'在线预订客',NULL,NULL),(222111252,'在线预订客',NULL,NULL),(222111253,'调试客','110101200002023456',NULL),(222111254,'在线预订客',NULL,NULL),(222111255,'在线预订客',NULL,NULL),(222111256,'在线预订客',NULL,NULL),(222111257,'在线预订客',NULL,NULL),(222111258,'在线预订客',NULL,NULL),(222111259,'调试客甲','110101200505051234',NULL),(222111260,'回归顾客','110101198808081234',NULL);
/*!40000 ALTER TABLE `client` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `core_property`
--

DROP TABLE IF EXISTS `core_property`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `core_property` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `code` varchar(32) NOT NULL,
  `name` varchar(120) NOT NULL,
  `timezone` varchar(64) NOT NULL,
  `currency` varchar(3) NOT NULL,
  `address` longtext NOT NULL,
  `status` varchar(16) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `core_property`
--

LOCK TABLES `core_property` WRITE;
/*!40000 ALTER TABLE `core_property` DISABLE KEYS */;
INSERT INTO `core_property` VALUES (1,'DEFAULT','默认酒店','Asia/Shanghai','CNY','','ACTIVE','2026-10-30 15:46:39.715734','2026-10-30 15:46:39.715734');
/*!40000 ALTER TABLE `core_property` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `django_admin_log`
--

DROP TABLE IF EXISTS `django_admin_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `django_admin_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `action_time` datetime(6) NOT NULL,
  `object_id` longtext,
  `object_repr` varchar(200) NOT NULL,
  `action_flag` smallint unsigned NOT NULL,
  `change_message` longtext NOT NULL,
  `content_type_id` int DEFAULT NULL,
  `user_id` int NOT NULL,
  PRIMARY KEY (`id`),
  KEY `django_admin_log_content_type_id_c4bce8eb_fk_django_co` (`content_type_id`),
  KEY `django_admin_log_user_id_c564eba6_fk_auth_user_id` (`user_id`),
  CONSTRAINT `django_admin_log_content_type_id_c4bce8eb_fk_django_co` FOREIGN KEY (`content_type_id`) REFERENCES `django_content_type` (`id`),
  CONSTRAINT `django_admin_log_user_id_c564eba6_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=12 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `django_admin_log`
--

LOCK TABLES `django_admin_log` WRITE;
/*!40000 ALTER TABLE `django_admin_log` DISABLE KEYS */;
INSERT INTO `django_admin_log` VALUES (1,'2022-03-22 05:57:59.100730','1','Vip object (1)',1,'[{\"added\": {}}]',14,1),(2,'2022-03-22 05:58:32.913980','1','Park object (1)',1,'[{\"added\": {}}]',11,1),(3,'2022-03-22 05:59:24.059057','1','Room object (1)',1,'[{\"added\": {}}]',12,1),(4,'2022-03-22 05:59:47.670863','1','Room object (1)',2,'[]',12,1),(5,'2022-03-22 06:01:48.842735','1','Client object (1)',1,'[{\"added\": {}}]',8,1),(6,'2022-03-22 06:02:25.695448','1','Food object (1)',1,'[{\"added\": {}}]',9,1),(7,'2022-03-22 06:02:53.655545','101','Order object (101)',1,'[{\"added\": {}}]',10,1),(8,'2022-03-22 06:09:30.199351','101','客房订单管理',1,'[{\"added\": {}}]',7,1),(9,'2026-10-28 14:06:53.337131','2','wwwww',1,'[{\"added\": {}}]',14,1),(10,'2026-10-29 15:54:22.920497','6','test',1,'[{\"added\": {}}]',4,1),(11,'2026-10-29 15:55:03.853873','6','test',2,'[{\"changed\": {\"fields\": [\"First name\", \"Last name\", \"Email address\", \"Groups\", \"User permissions\"]}}]',4,1);
/*!40000 ALTER TABLE `django_admin_log` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `django_content_type`
--

DROP TABLE IF EXISTS `django_content_type`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `django_content_type` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_label` varchar(100) NOT NULL,
  `model` varchar(100) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `django_content_type_app_label_model_76bd3d3b_uniq` (`app_label`,`model`)
) ENGINE=InnoDB AUTO_INCREMENT=23 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `django_content_type`
--

LOCK TABLES `django_content_type` WRITE;
/*!40000 ALTER TABLE `django_content_type` DISABLE KEYS */;
INSERT INTO `django_content_type` VALUES (1,'admin','logentry'),(3,'auth','group'),(2,'auth','permission'),(4,'auth','user'),(5,'contenttypes','contenttype'),(7,'hotelapp','accommodation'),(18,'hotelapp','auditlog'),(8,'hotelapp','client'),(15,'hotelapp','financerecord'),(17,'hotelapp','folio'),(9,'hotelapp','food'),(19,'hotelapp','ledgerentry'),(16,'hotelapp','nightaudit'),(10,'hotelapp','order'),(11,'hotelapp','park'),(20,'hotelapp','payment'),(21,'hotelapp','reservation'),(12,'hotelapp','room'),(22,'hotelapp','roomtypeinventory'),(13,'hotelapp','staff'),(14,'hotelapp','vip'),(6,'sessions','session');
/*!40000 ALTER TABLE `django_content_type` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `django_migrations`
--

DROP TABLE IF EXISTS `django_migrations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `django_migrations` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app` varchar(255) NOT NULL,
  `name` varchar(255) NOT NULL,
  `applied` datetime(6) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=35 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `django_migrations`
--

LOCK TABLES `django_migrations` WRITE;
/*!40000 ALTER TABLE `django_migrations` DISABLE KEYS */;
INSERT INTO `django_migrations` VALUES (1,'contenttypes','0001_initial','2022-03-21 12:39:46.358777'),(2,'auth','0001_initial','2022-03-21 12:39:47.243488'),(3,'admin','0001_initial','2022-03-21 12:39:47.468309'),(4,'admin','0002_logentry_remove_auto_add','2022-03-21 12:39:47.481960'),(5,'admin','0003_logentry_add_action_flag_choices','2022-03-21 12:39:47.496090'),(6,'contenttypes','0002_remove_content_type_name','2022-03-21 12:39:47.628961'),(7,'auth','0002_alter_permission_name_max_length','2022-03-21 12:39:47.703590'),(8,'auth','0003_alter_user_email_max_length','2022-03-21 12:39:47.783079'),(9,'auth','0004_alter_user_username_opts','2022-03-21 12:39:47.796093'),(10,'auth','0005_alter_user_last_login_null','2022-03-21 12:39:47.867664'),(11,'auth','0006_require_contenttypes_0002','2022-03-21 12:39:47.875785'),(12,'auth','0007_alter_validators_add_error_messages','2022-03-21 12:39:47.889429'),(13,'auth','0008_alter_user_username_max_length','2022-03-21 12:39:47.966852'),(14,'auth','0009_alter_user_last_name_max_length','2022-03-21 12:39:48.045694'),(15,'hotelapp','0001_initial','2022-03-21 12:39:48.054417'),(16,'hotelapp','0002_auto_20220321_2039','2022-03-21 12:39:49.548506'),(17,'sessions','0001_initial','2022-03-21 12:39:49.636921'),(18,'auth','0010_alter_group_name_max_length','2026-10-28 13:15:07.158031'),(19,'auth','0011_update_proxy_permissions','2026-10-28 13:15:07.162356'),(20,'auth','0012_alter_user_first_name_max_length','2026-10-28 13:15:07.194229'),(21,'hotelapp','0003_alter_accommodation_options_alter_client_options_and_more','2026-10-28 13:15:07.210264'),(22,'hotelapp','0004_alter_client_身份证号_alter_food_菜名_alter_park_车辆牌号_and_more','2026-10-28 16:35:47.845853'),(23,'hotelapp','0005_role_groups','2026-10-29 15:45:13.048364'),(24,'hotelapp','0006_financerecord_accommodation_冲账人_accommodation_冲账时间_and_more','2026-10-29 16:08:09.597740'),(25,'hotelapp','0007_order_冲账人_order_冲账时间_order_状态','2026-10-29 16:09:11.759244'),(26,'hotelapp','0008_nightaudit_accommodation_押金','2026-10-29 17:35:18.811452'),(27,'hotelapp','0009_folio','2026-10-29 17:48:21.852621'),(30,'core','0001_initial','2026-10-30 15:46:39.719167'),(31,'inventory','0001_initial','2026-10-30 15:46:40.287877'),(32,'guests','0001_initial','2026-10-30 15:46:40.359483'),(33,'reservations','0001_initial','2026-10-30 15:46:40.917875'),(34,'billing','0001_initial','2026-10-30 15:46:41.510714');
/*!40000 ALTER TABLE `django_migrations` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `django_session`
--

DROP TABLE IF EXISTS `django_session`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `django_session` (
  `session_key` varchar(40) NOT NULL,
  `session_data` longtext NOT NULL,
  `expire_date` datetime(6) NOT NULL,
  PRIMARY KEY (`session_key`),
  KEY `django_session_expire_date_a5c62663` (`expire_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `django_session`
--

LOCK TABLES `django_session` WRITE;
/*!40000 ALTER TABLE `django_session` DISABLE KEYS */;
INSERT INTO `django_session` VALUES ('0e0uwyw4egvoujhk5kychk3osobyk5ce','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUrH:6rxkO8eOjf7_U9BDM_13umaswOfOF6AvQGEfYoRPa-U','2026-11-12 18:28:15.371803'),('0k4ncyzrpp7ejgi2ilfsxkkfejlsojlj','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUqg:TU9bdHHIdGsGN3AZkmlTEupPFJFQOoZD4Pr9WX4gM8I','2026-11-12 18:27:38.464952'),('0ocqf5hhnfz48mygam00tnfkw53arqpq','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMTY8:Mu0lzNFem63Fo-vGNL45ufGYhpgkXSLSYpI7ucqApbI','2026-11-12 17:04:24.561139'),('1bo1wf3zxj0q7mwcuwnj7h4gses6mgda','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUrH:uWZoBJzW76Mga7eIOD0IPXfTtPW3MU5rFglYtPjmm1Y','2026-11-12 18:28:15.165350'),('1bo4m27r2uw3bjkcns7abr8p7ihl0f6r','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUkl:6oiEMLe280ZAFsj_9adz_f1rZeSMQfxew_vOjP8xVd4','2026-11-12 18:21:31.662403'),('1fjht4oldmxq4vm6ve74w5ttwaspm052','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUmL:vfLjmaeA2sU7naNy-G9jKV_At3kGLddzEHOhbPJNKzE','2026-11-12 18:23:09.401007'),('1s1nf2mrbr5tg8mvaf41fgd19j2fsi9q','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUoX:qky_QFkPs9zr0CGjRCb7M59jysOP5O0IO6BiMvZMpPo','2026-11-12 18:25:25.356093'),('26v48xqbgy1ng5xow2a4uqfwvl8136bd','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMU8X:65orzO2o3ty1jwvd-cdJfaBr1FPGAq7ncJFq56rQJ88','2026-11-12 17:42:01.752134'),('2b6x5skulkqx9ibua9ruq2enrmdrk7ms','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSxa:Qs8BmL-_i0A4kQMo0T96v2h1-EQQRdz6cifWA_6V5R8','2026-11-12 16:26:38.466224'),('2f6pe07llj7ssmu72z3uljpv0lnx2zzq','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUdP:1v4B-Hjz90jIYYUQdVHYVTzYcoR6jy7jkWXFVftY8lc','2026-11-12 18:13:55.495723'),('2tk2dx8rde3wwvq300qdlxl6q9dae3tx','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUjG:asWKgf-g1PJv5Q5Xk4FhVqvgPoi2eK3wX3jJsJyRI0c','2026-11-12 18:19:58.884184'),('2vdzsh62wt7ipv60kn6kutboekxpus1l','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUpD:LLEkOxluYGkAzkJu03i_H-cJ7HTwEg3AcjSLUou6p3U','2026-11-12 18:26:07.154361'),('31d7ls4a5puybebso9h5pjo8pk8e66re','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUa5:z5IpCHlyjZm0PwWsdRIxZEN3kP5jeiDnotE-7zgwL50','2026-11-12 18:10:29.498049'),('3yreutbyjcw9a7o42o9bvbkujate29pq','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUo4:xAGRjP97zipryLdWwxYouwQmFER7FsSYVNMml2GgsUg','2026-11-12 18:24:56.034919'),('4k3onemjryt0ldh49lwgh6j8xendouli','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMSNh:LPlW3b4SY9kVihPHe2bImUxY85eeBUmzQqf6Bnqj404','2026-11-12 15:49:33.882965'),('55fzjt34u1fq1b1m5ljw9w6riy5oqcq8','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUpp:H3czoklOgpzlBPDm-b5lw2e7Kxn0O7hLW_v-3ADS39I','2026-11-12 18:26:45.327081'),('57is16gch7xmvwks15xu5hdju8pwvk5j','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUmL:LwjtLYustSd89WXnNB4CaGeWYM91FjN9Foxu_qgFWjE','2026-11-12 18:23:09.630758'),('5gj6kmgp6daoc2khmgc7wpojdi30mb6n','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSxc:btE6LTai-uT7tsf-lW_RaTpZrB10DT-iWMRHs-Mc5SU','2026-11-12 16:26:40.556636'),('61qpse1rauyhsrfcjlouzjnthsxr5z5f','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSxd:gaBDjaN1jr7zLpnWZpb-9huTM6yGTkr9m3eQ456Tzh4','2026-11-12 16:26:41.191691'),('679sflpyqcxbv1uyfcyy8t1j4378g5p4','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUMV:3t2KfZDBZgqg3d6Xp87tTc7sDKUa8MGblfpldJQUbf4','2026-11-12 17:56:27.159735'),('6cmiz4f0n6wzvfby8ps0i8aucquy9l5i','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMSvC:eNFbqjQ0sCvPmL5oySKuqd1eYAPUS6vQZTx0_NO7dxc','2026-11-12 16:24:10.978313'),('6fim8da6nsehsetuxvbh9vd4bptxacuv','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUhz:FxkAShlHu3Qy-50SVZB36-88OwyLLZdnIlTb3fUdnAE','2026-11-12 18:18:39.624525'),('6t85x3yqojnplp7yftdyz1rrrhdru7xz','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSvB:kslOihyVrYFKmXZSswnpSvEsSnsqBpEN5TrisYbxCa8','2026-11-12 16:24:09.905374'),('748jlgoqy3fnz45hlkwwt56ay1g3qn8m','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUg0:yqSGfXVzCMy7UEUUK9BLlVJB0ZRH_P6MQh6ZfCrzeeU','2026-11-12 18:16:36.675740'),('7dwd6pwmsdi96i5l4y6xao7i2i84e522','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUpU:U9Dvy2K3modlY8JeF7I052EvgHWnllXTu_lON_0TaG0','2026-11-12 18:26:24.461129'),('7e3gu0atvoick5ltbgkc9qojlli27vbv','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUpU:8_wf5Bi831CPlozS6o7O6SOppeM5j40NhVC5psIyDOI','2026-11-12 18:26:24.260406'),('7e3szukgmyrcnqrfdercznsihezolnhk','.eJxVjEEOwiAQRe_C2hAY6FBcuvcMBAZGqoYmpV0Z765NutDtf-_9lwhxW2vYelnClMVZaHH63VKkR2k7yPfYbrOkua3LlOSuyIN2eZ1zeV4O9--gxl6_9eCc1YoYkQwAg2LC5IEKFO88J2IbzegsZJUUKo9am5HQARs1MHrx_gDRVzcb:1xMhiK:_SVn2AkcbdTl6IdjgMnIlPFyC0jkU2oGglYETKwRfV4','2026-11-13 08:11:52.847589'),('7err49nszrorw5mcdmixwqtz0lbxtjxb','.eJxVjEEOwiAQRe_C2hAY6FBcuvcMBAZGqoYmpV0Z765NutDtf-_9lwhxW2vYelnClMVZaHH63VKkR2k7yPfYbrOkua3LlOSuyIN2eZ1zeV4O9--gxl6_9eCc1YoYkQwAg2LC5IEKFO88J2IbzegsZJUUKo9am5HQARs1MHrx_gDRVzcb:1xMnzM:lSlWHUtRomKXe5Uk5IRyDzl2d2CGeElmlFrjmr4TJhc','2026-11-13 14:53:52.980736'),('7escocex34v8zhlzrfnt6qqotj1hr8yx','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUjG:3-PtlnRoZAOahE_FRSozDZsW3W0dbgbkfOnQoOan0V4','2026-11-12 18:19:58.668829'),('7hwtv65giqqj17i8pnu8yngq85n4okz1','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUi0:M1Dd99P2XFCul-6o7cdijgxS3iKHHkNM-pB2fnyIJGo','2026-11-12 18:18:40.102303'),('7lwswptmk2awvfng66rhdsd57tgaad6h','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUmK:fZw12MNc9CWTofyY86xDmDmTScVM8wEZoL_YZBM7bUY','2026-11-12 18:23:08.978174'),('7vnr7rmkdcgsraki6b6suwug8ugcppii','.eJxVjDsOwjAQBe_iGlnYrJOYkp4zRPvx4gCypTipEHeHSCmgfTPzXmbEdcnj2tI8TmLOBszhdyPkRyobkDuWW7VcyzJPZDfF7rTZa5X0vOzu30HGlr81aWSmwTN45U6ZGcA5BNd3QVGOwOpZHPjYx05QKagg9achoMMhoXl_ABjXOUo:1xMSKI:qAjfh8tmTAtfo4ml755LO_NX2Pn98eG0kkrNuTayjJM','2026-11-12 15:46:02.563395'),('7xlxqs2vf24df9f0cej1x7kjbena7wr6','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUg0:FRXaivdeOskbNLnFu4t7FyR75EP-x6uvjx-pcQ6XTqc','2026-11-12 18:16:36.448725'),('8ew15ruoat14cvc2g2x932e72qgk3cop','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUhz:1ih5E9eWEA0W9oK7MFVY8rR-FpMhZ5Xi5nMxPKbCh8U','2026-11-12 18:18:39.867658'),('8frczfrr8mf43180hmg2s4gj9vohrg6y','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUqH:x4I81bwCafQCoclvgVCAEAyRrEhBQEpZthON5Ji52IQ','2026-11-12 18:27:13.034880'),('96cn0nabt5rkl686eb3gddpyvrcvuivc','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMTRT:SkC318e_9REGo6W0oRnv7EAU8Fn-sxIQ26kxDe63eLo','2026-11-12 16:57:31.820951'),('9fimr8k2b734u45t7ocfoagfzuu8cwc6','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSw7:RrT4JeB3zDZRsOhfs3aWfYqrqgZIe8boEGLb9m3wSag','2026-11-12 16:25:07.738970'),('9z79s5n52sw1gwg3llurkcot9sn1vbb7','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUkR:bPDNaUD7Woi7Io7K0j4MPbmX16iltdO61OCB3vnWfqk','2026-11-12 18:21:11.689345'),('a7iy8igdkcmsgt818e9nha576y8sr1i7','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMSys:Qib9aof0LauRma29UEnsbXUhRJkPw5Zgbdlhz8m_2zk','2026-11-12 16:27:58.056500'),('ad6fkxgsqaj7xv7z8i2c584s0oridos2','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUN3:E1KTBmU0o2L_Z39PlbSgml6MEjv_gfRrgrSK5Wo0C70','2026-11-12 17:57:01.424491'),('aldt5e7hgq9jt0xfx6qt9ka2alsvtmox','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUpp:h8gFbEJgxH-S6qJkQWPBOcQM2qtzK0U8DOw8_fM_LsM','2026-11-12 18:26:45.729951'),('atg33uyadlx2fqgoev4cm5xfdjliap5m','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUjG:JA2w8fSOgl_uWdRs7O5edY2vg16oD6ZuGHa8AoWOpjI','2026-11-12 18:19:58.448262'),('aufoaina1wza5hb85oozehh8ceusorhe','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMSxb:tS4SUBNv2wdLEQwq_7PNDVtnGPAMUuMore2eZc6KbmM','2026-11-12 16:26:39.894678'),('bfxkrao4togickvns6v6a17mc278syh7','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSyt:MIWZU7MEDNpiWcYnqdl2NF69QMclRPZodnS62KFirCo','2026-11-12 16:27:59.846272'),('bhnilzrb7p3hdb2xrnlg36485nst6yxz','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUsC:YM8Z17jdBcebGEF3Cxn551x0pfPNUABfAFIYA2CV98U','2026-11-12 18:29:12.342012'),('bu359xl0mu39stx94t7jgft9qegcivdz','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMSNg:oz23VgUnZLDTKZR0aAPboPwh8RwvorWjPjXvk1GInOc','2026-11-12 15:49:32.265557'),('bvhrqsjq4vm53y4khstwz8nglz5arfdm','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUpp:KyUhDltCS9xmX8P6CnBPcJHpZ_7HzQ1XkGC8-4eRNUk','2026-11-12 18:26:45.945927'),('cafrkgwzrtcr9z8euipaof0a2his6v27','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUmL:nIMJWgXJFYqYb-OXWKTJc4OZ5_AAsv3L2lxdo1aAdA0','2026-11-12 18:23:09.197912'),('cat9pd2017mm6zfadqu308o0mlqw0ch4','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUqH:fDOo0qiQKVHxz4GXGiXu1KLP1sIXrV5truVCr84iTpc','2026-11-12 18:27:13.236167'),('cgd24bczul7g7vnrpkkrrcsfmccykklt','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSNf:79sqrV3bsJ_9aRbttoHb8jb4OIBP9OWazoRRJNYTDSc','2026-11-12 15:49:31.023905'),('co7d6je741zv93e9nribzxoirq1kkkyo','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUqq:IgxCuMV66JGGmTVVvirKpLw8Lf2FwN3mHQHsUSOPVv4','2026-11-12 18:27:48.879486'),('coq1gdsytk7cnb7iwt6h1ossw8m19fs8','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUg1:BlllxHtsDqwM9i-FqqTxX4Z3757PwFnssFwf3rw2wdU','2026-11-12 18:16:37.340028'),('cq7qlidnio2aaxemuxvxmo0sxoaqilli','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMRuz:8KwSiL_rxaS8RmQ_JU73SDLN9hpTlgx9MSTqyvc-o_0','2026-11-12 15:19:53.684929'),('cwnetfn2mxx6x8qbbapdypgbnfynqxgs','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMSNi:3ChGiTq155ViZrtt65ovZJKnMgnl8CT0CxPNTVqjy5M','2026-11-12 15:49:34.678325'),('cxb8s12qxh9erwdtqb3zvv8j9p2mglho','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMSvc:prW4z46JkCZRgyeTfBs0jyHaf7maSPRz5tWzuZ7PT8c','2026-11-12 16:24:36.866760'),('d3qt3xcswv7mkch9xaw3f5pzixaxoswp','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xM4s4:zIe02pPEOirB0x5FGw6nFLpHuWns0l_AfjDtqfCbc-Q','2026-11-11 14:43:20.926995'),('dheu4vn1lb8vo08yn78fcvfn2azi4e1o','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUOV:YxC1B7BKLeNlFR7_2FGZCJC5cQhKnLjloD7eJfmKe84','2026-11-12 17:58:31.477676'),('dhwnmtzt5dwxr3laoecvvayp9ywcg1lw','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUdP:Pqn6b-xwjG8A1Il2vw2x9HwaWsH9n-BRo47UPLPC3Fo','2026-11-12 18:13:55.288491'),('eke7o03h6jsiuklwovkbobhn0ivhb43k','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMU7U:E8mmFgjdeCaDieGbzI1aV5geZfGM70yiYqopZltxbE0','2026-11-12 17:40:56.979294'),('eojs95iekkwsqw6au4b2ekwgc03ja14z','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUo4:qkzZAOLI70Cmo84A_3Zi6gpwQQcBrKnmhPvlu4N4Ftk','2026-11-12 18:24:56.243596'),('exhwzkpr1orarxzmqtd4ef6mke2nyjwu','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUpp:ZKAx4dO1IqXjPdaWFS-e4tCf5rYO8ZEgQxMnwN9JmnY','2026-11-12 18:26:45.108431'),('f8rpdcrymwtsx5ice8lkxm00499rogct','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUr0:uGEdObnARR599qLk2nnemtED8ffznOzrpwDiVEorWCg','2026-11-12 18:27:58.689749'),('fd3xc5gljhytcknhn6bdsodaghak1giz','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMU9r:-jutsj9dUAzuvlTTnLSvuRMsdTnk_xAnMu7fMoMyWsA','2026-11-12 17:43:23.713107'),('fge7n7xfn9o5lxnisrg4i2hvs81ab3g7','YjA1YjBmMzIyYWM1MDA4Y2Q5MWNiYjRmMzQ1YzhmMDJjZTgyOTNmNDp7Il9hdXRoX3VzZXJfaWQiOiIxIiwiX2F1dGhfdXNlcl9iYWNrZW5kIjoiZGphbmdvLmNvbnRyaWIuYXV0aC5iYWNrZW5kcy5Nb2RlbEJhY2tlbmQiLCJfYXV0aF91c2VyX2hhc2giOiI2YTAxZDQzMmFmYzA0ZjA3ZjdjZjQ1OWQzYzgwMTViMDA5ZGYzNzVhIiwiX21lbnVzIjoiW3tcIm5hbWVcIjogXCJIb3RlbGFwcFwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCIsIFwibW9kZWxzXCI6IFt7XCJuYW1lXCI6IFwiXFx1NGYxYVxcdTU0NThcXHU0ZmUxXFx1NjA2ZlxcdTdiYTFcXHU3NDA2XCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIiwgXCJ1cmxcIjogXCIvYWRtaW4vaG90ZWxhcHAvdmlwL1wiLCBcImFkZFVybFwiOiBcIi9hZG1pbi9ob3RlbGFwcC92aXAvYWRkL1wiLCBcImJyZWFkY3J1bWJzXCI6IFt7XCJuYW1lXCI6IFwiSG90ZWxhcHBcIiwgXCJpY29uXCI6IFwiZmFyIGZhLWNpcmNsZVwifSwge1wibmFtZVwiOiBcIlxcdTRmMWFcXHU1NDU4XFx1NGZlMVxcdTYwNmZcXHU3YmExXFx1NzQwNlwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCJ9XSwgXCJlaWRcIjogMTAwMn0sIHtcIm5hbWVcIjogXCJcXHU1MDVjXFx1OGY2NlxcdTU3M2FcXHU3YmExXFx1NzQwNlwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCIsIFwidXJsXCI6IFwiL2FkbWluL2hvdGVsYXBwL3BhcmsvXCIsIFwiYWRkVXJsXCI6IFwiL2FkbWluL2hvdGVsYXBwL3BhcmsvYWRkL1wiLCBcImJyZWFkY3J1bWJzXCI6IFt7XCJuYW1lXCI6IFwiSG90ZWxhcHBcIiwgXCJpY29uXCI6IFwiZmFyIGZhLWNpcmNsZVwifSwge1wibmFtZVwiOiBcIlxcdTUwNWNcXHU4ZjY2XFx1NTczYVxcdTdiYTFcXHU3NDA2XCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIn1dLCBcImVpZFwiOiAxMDAzfSwge1wibmFtZVwiOiBcIlxcdTU0NThcXHU1ZGU1XFx1NGZlMVxcdTYwNmZcXHU3YmExXFx1NzQwNlwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCIsIFwidXJsXCI6IFwiL2FkbWluL2hvdGVsYXBwL3N0YWZmL1wiLCBcImFkZFVybFwiOiBcIi9hZG1pbi9ob3RlbGFwcC9zdGFmZi9hZGQvXCIsIFwiYnJlYWRjcnVtYnNcIjogW3tcIm5hbWVcIjogXCJIb3RlbGFwcFwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCJ9LCB7XCJuYW1lXCI6IFwiXFx1NTQ1OFxcdTVkZTVcXHU0ZmUxXFx1NjA2ZlxcdTdiYTFcXHU3NDA2XCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIn1dLCBcImVpZFwiOiAxMDA0fSwge1wibmFtZVwiOiBcIlxcdTViYTJcXHU2MjNmXFx1NGZlMVxcdTYwNmZcXHU3YmExXFx1NzQwNlwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCIsIFwidXJsXCI6IFwiL2FkbWluL2hvdGVsYXBwL3Jvb20vXCIsIFwiYWRkVXJsXCI6IFwiL2FkbWluL2hvdGVsYXBwL3Jvb20vYWRkL1wiLCBcImJyZWFkY3J1bWJzXCI6IFt7XCJuYW1lXCI6IFwiSG90ZWxhcHBcIiwgXCJpY29uXCI6IFwiZmFyIGZhLWNpcmNsZVwifSwge1wibmFtZVwiOiBcIlxcdTViYTJcXHU2MjNmXFx1NGZlMVxcdTYwNmZcXHU3YmExXFx1NzQwNlwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCJ9XSwgXCJlaWRcIjogMTAwNX0sIHtcIm5hbWVcIjogXCJcXHU1YmEyXFx1NjIzZlxcdThiYTJcXHU1MzU1XFx1N2JhMVxcdTc0MDZcIiwgXCJpY29uXCI6IFwiZmFyIGZhLWNpcmNsZVwiLCBcInVybFwiOiBcIi9hZG1pbi9ob3RlbGFwcC9hY2NvbW1vZGF0aW9uL1wiLCBcImFkZFVybFwiOiBcIi9hZG1pbi9ob3RlbGFwcC9hY2NvbW1vZGF0aW9uL2FkZC9cIiwgXCJicmVhZGNydW1ic1wiOiBbe1wibmFtZVwiOiBcIkhvdGVsYXBwXCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIn0sIHtcIm5hbWVcIjogXCJcXHU1YmEyXFx1NjIzZlxcdThiYTJcXHU1MzU1XFx1N2JhMVxcdTc0MDZcIiwgXCJpY29uXCI6IFwiZmFyIGZhLWNpcmNsZVwifV0sIFwiZWlkXCI6IDEwMDZ9LCB7XCJuYW1lXCI6IFwiXFx1OTg3ZVxcdTViYTJcXHU3YmExXFx1NzQwNlwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCIsIFwidXJsXCI6IFwiL2FkbWluL2hvdGVsYXBwL2NsaWVudC9cIiwgXCJhZGRVcmxcIjogXCIvYWRtaW4vaG90ZWxhcHAvY2xpZW50L2FkZC9cIiwgXCJicmVhZGNydW1ic1wiOiBbe1wibmFtZVwiOiBcIkhvdGVsYXBwXCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIn0sIHtcIm5hbWVcIjogXCJcXHU5ODdlXFx1NWJhMlxcdTdiYTFcXHU3NDA2XCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIn1dLCBcImVpZFwiOiAxMDA3fSwge1wibmFtZVwiOiBcIlxcdTk5MTBcXHU1Mzg1XFx1N2JhMVxcdTc0MDZcIiwgXCJpY29uXCI6IFwiZmFyIGZhLWNpcmNsZVwiLCBcInVybFwiOiBcIi9hZG1pbi9ob3RlbGFwcC9mb29kL1wiLCBcImFkZFVybFwiOiBcIi9hZG1pbi9ob3RlbGFwcC9mb29kL2FkZC9cIiwgXCJicmVhZGNydW1ic1wiOiBbe1wibmFtZVwiOiBcIkhvdGVsYXBwXCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIn0sIHtcIm5hbWVcIjogXCJcXHU5OTEwXFx1NTM4NVxcdTdiYTFcXHU3NDA2XCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIn1dLCBcImVpZFwiOiAxMDA4fSwge1wibmFtZVwiOiBcIlxcdTk5MTBcXHU1Mzg1XFx1OGJhMlxcdTUzNTVcXHU3YmExXFx1NzQwNlwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCIsIFwidXJsXCI6IFwiL2FkbWluL2hvdGVsYXBwL29yZGVyL1wiLCBcImFkZFVybFwiOiBcIi9hZG1pbi9ob3RlbGFwcC9vcmRlci9hZGQvXCIsIFwiYnJlYWRjcnVtYnNcIjogW3tcIm5hbWVcIjogXCJIb3RlbGFwcFwiLCBcImljb25cIjogXCJmYXIgZmEtY2lyY2xlXCJ9LCB7XCJuYW1lXCI6IFwiXFx1OTkxMFxcdTUzODVcXHU4YmEyXFx1NTM1NVxcdTdiYTFcXHU3NDA2XCIsIFwiaWNvblwiOiBcImZhciBmYS1jaXJjbGVcIn1dLCBcImVpZFwiOiAxMDA5fV0sIFwiZWlkXCI6IDEwMDF9LCB7XCJuYW1lXCI6IFwiXFx1OGJhNFxcdThiYzFcXHU1NDhjXFx1NjM4OFxcdTY3NDNcIiwgXCJpY29uXCI6IFwiZmFzIGZhLXNoaWVsZC1hbHRcIiwgXCJtb2RlbHNcIjogW3tcIm5hbWVcIjogXCJcXHU3NTI4XFx1NjIzN1wiLCBcImljb25cIjogXCJmYXIgZmEtdXNlclwiLCBcInVybFwiOiBcIi9hZG1pbi9hdXRoL3VzZXIvXCIsIFwiYWRkVXJsXCI6IFwiL2FkbWluL2F1dGgvdXNlci9hZGQvXCIsIFwiYnJlYWRjcnVtYnNcIjogW3tcIm5hbWVcIjogXCJcXHU4YmE0XFx1OGJjMVxcdTU0OGNcXHU2Mzg4XFx1Njc0M1wiLCBcImljb25cIjogXCJmYXMgZmEtc2hpZWxkLWFsdFwifSwge1wibmFtZVwiOiBcIlxcdTc1MjhcXHU2MjM3XCIsIFwiaWNvblwiOiBcImZhciBmYS11c2VyXCJ9XSwgXCJlaWRcIjogMTAxMX0sIHtcIm5hbWVcIjogXCJcXHU3ZWM0XCIsIFwiaWNvblwiOiBcImZhcyBmYS11c2Vycy1jb2dcIiwgXCJ1cmxcIjogXCIvYWRtaW4vYXV0aC9ncm91cC9cIiwgXCJhZGRVcmxcIjogXCIvYWRtaW4vYXV0aC9ncm91cC9hZGQvXCIsIFwiYnJlYWRjcnVtYnNcIjogW3tcIm5hbWVcIjogXCJcXHU4YmE0XFx1OGJjMVxcdTU0OGNcXHU2Mzg4XFx1Njc0M1wiLCBcImljb25cIjogXCJmYXMgZmEtc2hpZWxkLWFsdFwifSwge1wibmFtZVwiOiBcIlxcdTdlYzRcIiwgXCJpY29uXCI6IFwiZmFzIGZhLXVzZXJzLWNvZ1wifV0sIFwiZWlkXCI6IDEwMTJ9XSwgXCJlaWRcIjogMTAxMH1dIn0=','2022-04-05 06:09:11.603463'),('fjr57ya9gb6ofcj16j5zks6fhqz5hfrn','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUqf:IR7OmEDdsJ9F2DA1FQ6wDePFwaLFMs87A9Kqctc35xw','2026-11-12 18:27:37.805561'),('fqbkq0754qa0qdqjjgestt9lodpia7lg','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUjG:ZdiSuk1NKXk2VaUP6H3JmSoNmAkNGSVn79fZjsSN-74','2026-11-12 18:19:58.224751'),('g0mncbrpj7hijr82408r7hkr1p9q63sc','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUo3:IF3ePkCUkVpGOJH13A9lTuM-4G-LpZsfILViSaYHubQ','2026-11-12 18:24:55.568470'),('g3bc2qigonpag3dthdpjm81g76ys8ipf','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUkQ:MetcTi9RHpw9jLBAH2oGIHpI2ga3ueDj-0I_g2ec_Pc','2026-11-12 18:21:10.987877'),('gch6z8tjxofpidtwh8tvytvcw55vkoqw','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUdP:94KPjPc7bsEbE66ynRaZ5cWzQzjAU92EwXHkvWBfQBU','2026-11-12 18:13:55.712724'),('gf0is4rjzmraldqzhx54jy0una31tusi','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUA9:DLUClNG5QQwLZBsgTv_3UVRzcVZHQqIOMQXXz8Hn9dA','2026-11-12 17:43:41.801346'),('gjgbfohhv6sck1u2zgu5e8dbfjlstqxs','.eJxVjDsOgzAQBe_iOrLAf1Km5wzWrteOSSJbwlBFuTsgUZD2zcz7Mg_rkv3a4uwnYndm2e26IYR3LAegF5Rn5aGWZZ6QHwo_aeNjpfh5nO7fQYaW91pYAJWcoyAJRZ9wELpTwmmjQooOUWmQJpjeWtQOaJCdipTMDjFpYdhvA_IDOHE:1xMSp1:gLdiFh6dS7poBt-oTqZ_M9dgV8QbswfdGonaO8l75rU','2026-11-12 16:17:47.906762'),('gly6ycnqom9011sjlncj5kgo4srs0peg','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUqg:87kYw2Ekx_8Yl92pe-PnjlfzlSmRZI10dQy5pvgsaq0','2026-11-12 18:27:38.022927'),('gr4jcjb79m0ekrpzf2q0m3gne0qtq8mo','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUpU:IJNnl_Eae1AQMlBfHmaYMwTuaVkHMknkF3RIE7jAm2Q','2026-11-12 18:26:24.056082'),('guu2ml38r19srd8unvikkd7zcttxxvx6','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMSw9:t8ZiMsXswDX0XYCM3e6pDpPEK2NJm33vUF5esXzePhc','2026-11-12 16:25:09.602759'),('h77i9pfn3lsp804zvpxld657jzv6dt2k','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUqq:CDeIR5gixkoT4_KIav51NJmBD8zZlbSDEtDAvAqX5_A','2026-11-12 18:27:48.459651'),('h7wvsqctgup62awxoramqmb1pjyupu79','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUhz:IsEn7w0IFWU_6Ng24rI1XtJnaKH-F1tyUHZpW0VNUOw','2026-11-12 18:18:39.165930'),('her7ae7iby82nxix29p0h3o05aqdodfo','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMSp3:Kqdijrz1u6tMA5ow1SVGquaXj1K0U2pOEtL5BuKYxbk','2026-11-12 16:17:49.200520'),('hihfth1tmbkx9hclkvxl6q3vnydq1elf','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSp2:QvKzwC1JjwC-WmnY2ON0-22SnEYsatg32gKLeW2dWtw','2026-11-12 16:17:48.531857'),('i3spc2qd8saups80jg1nn534gjozprlq','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUr0:f8oCO-AQvbSiT83ZqKJ5O6rrhoE2Ef_lqmcIPV_-xIc','2026-11-12 18:27:58.911578'),('i4fd94rqh7xktstsko7uqc67c8e80390','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUoR:o2vC3jvBazt9rabaXXPeqNoWh2v0U_75jCMVOCUMT8w','2026-11-12 18:25:19.027586'),('ih57xdktryigc13pio2utsa2xcw0rxly','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMU8Y:e0UpA1lbk6AICoB-up08UXKrrrs0GZqGia4jMuCV1K8','2026-11-12 17:42:02.475701'),('imtbescf08nwxkoijbuwja51d94coth0','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUtN:aUCw7V-_mVmXnOJ4S03n93vxvPz-V0lGh4buLDrqGQU','2026-11-12 18:30:25.051649'),('jb8ejdhpplvy3sk4les3u3jre1az185i','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUrH:IMJtBuXbOUu6F7OOg9WsQ8gxywLzifERbgTMQOBFvOo','2026-11-12 18:28:15.781358'),('jn8675j2aewx8nk3r25s9eoxdcn87atx','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUr0:Hp-EZxSeh95lzvgs9cx2Uxe1fFTUeGzZv-KaqFxY24o','2026-11-12 18:27:58.253284'),('k3jy2axiugk62hhdm7ym7o7er6ik09ig','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUo4:Pita7pbVfYv4blV7XN2SPGj-_YWHm2dn3zq1N_4CbxE','2026-11-12 18:24:56.468307'),('k4dpqlve9ncwfv5g6uetvxzh4p848nwh','.eJzFl01zmzAQhv-Kh7M_kISQyLGnXnrsqWQ8iyRsGkAeAb1k_N8rgWcSU-xoHEhPGmlf9tU-y4d4DfbQtcd91yizL2TwFKBg_X4tA_GiaheQv6E-6K3QdWuKbOsk20u02f7QUpXfLtqrBEdojvZqjFGCWZ5jKZhMBBZc8YjzCAHDIZERz-MMM84oKMwhI3EseEIkyQTCiJDQJa1U3TU216_XNKihUmnwtEqD77pVJZxOabC2s8Lub1jPwaxy2IjCiFINwcrtsnHh6xRp2kU5AjvQiPJ-ppAd4jDO7cAycDMWhbGHSWfKIbYDWRX17njZ3-5PcdoNEpDy5z2VjV-UmVEghemqbGLbnpWf16tlqj0_u7AqpIuiMMQTTjSkwg62vbGbMQLzAT2BefEg2ssWR_rJQscsyZTF0C4qFV3qHm1ayHMPpoNueaizVDxmG005ZYBdbkzypdgarSsPtL1sebKz1DsmSz904sOMEkrnIwtC6Mq-3KEtdO2B-Fr_xawfJTBmHU84JZypN8OZ6IqyUHXrgfUiXJzno1WOCbKp3AkK--7wGe_PXGvpwa-XLU_vwQrH9PiHuRd51rWRynjAHHRfTHOuZzsZL6AJa2sW9YNA_aeSu_NHTLj7YsYsIv96Ns6zORaqlBso2w8PxYxiPry42I0C3BF_umHuD2Dnwrd79SbxbNM8RU-g9C101BY01RamRHRjFy5JsxH6cAfZwejuzn_CO81_h-ZV6BgZHi-E5-fg_BeasYxO:1xM3Va:Vt-L6oBmVHJgqS_H6Xi7dywlx9Fm8ZYijMhOEf6wsYo','2026-11-11 13:16:02.049117'),('kmzh3gctsnz1isrtir37jgyvk4a5otw2','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUg0:TZERQrMMiVpZenM4EwkpgL2RMxby-UP1ZMCbofg8-yc','2026-11-12 18:16:36.908949'),('lepqubw0zmazew75bqwm8ei8llexhjqe','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUoQ:Nsjn07QM2QPSHoGRhntHh0s9XAPD3Oc5NkUNG8g3PKg','2026-11-12 18:25:18.142094'),('ltdpbdxt3mlgfpnki2glt7synsl89jsc','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUpD:7HR9id7mqKiXhZo41exa4milmm28MFxH5c8zmNtbmjA','2026-11-12 18:26:07.836260'),('lvfs9ovozxkx4zrvlgv27tqoeyw0np8m','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUg1:7ZHwIiGfez00YSy4cP8STpsLidLNgRXon7Y5YDszEJY','2026-11-12 18:16:37.112063'),('m7cjteh5rigbx6hlllhsc53e8rmamoma','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUKB:ZAiLC7ojcZoPJSyf84LtG8YDVD9ZHSP9RNhCeAq_PFk','2026-11-12 17:54:03.884135'),('mcketcwb8or74j1z1qifg8441bsm5fyp','.eJy9lE2PmzAQhv9KxDlNzIexs8eeeumxp7KKxva4cRfsyEAvq_z32gGpDUVdtIWeRuN5mY_HY16TM_Td5dy36M9GJU9Jlux_PxMgX9DGgPoO9ps7SGc7b8QhSg5jtD18dgrrj6P2IcEF2kv4uhQnolWh84ITyXOSpgRSQGRc8ExzLARF0JronJwyKTPBCsY0kTTDXGvJY9IGbd-GXF9fq8RCg1XytKuST67DGq7XKtkHz4T-hnMNfqfhgzRe1jgEm9hlG8OPKaqqL3QKwdCC8ruHaTAlKXUwTED0WEHKBUV6Xw-xI6jG2ONl7O_4w1yPgwSU-vI3VYiPSuERlPR9I2baXjj5bb_bZtrbcwyjUTGaEpLNVKKEymC4LsvosRzWA3oF_7KA6F22OdJ_HHTKMp8rISCL95Tleqsd9c41fyC1fV1vSW6VsaYAizcr8cGjOaXrAQQpXRP-M9AZZxcs56N--y1dhcCUNZ2pdOIMfxVcia6sDdpuAdZRuDnP9045JVjO5T6l5H47fMX91M6p__3A3zvIFBJ7M_cmT9p5hX7Bzg267VdulYmnbPn0IL09J7ef4sIuAA:1xMV2n:YEiR7W822_jXE2uD7U3MCpjyQcarLlVwCToKy4VBCmc','2026-11-12 18:40:09.494739'),('mj3wi107jrnlbzk5b3ffsli8wxxcq4p9','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUqf:K-0Hjp35QmlTkKHKm5w_rVCuz2T_qwj4W8M-iTFxgHo','2026-11-12 18:27:37.603053'),('mzz19c11v28k5anc0ruscccxpwlg6rfy','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMULd:x98ObogHuiH6oMMFl9GD72LI-5oKh3PtESY2Uml95Mw','2026-11-12 17:55:33.771782'),('n43y8jzvqv2hjh3p4c4bab6nqqdujtec','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUkR:vQes6SBeB_caM9aew0x8Qz-DmOJ_iS4xkrFIUdZRQ48','2026-11-12 18:21:11.454239'),('njb1lt7cx0enyy5f4ape7m178dkfzlgb','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSva:q3y_za1U_JIVfcCLbHlx7zN_qpECq5n8kt9QuDCh_Ac','2026-11-12 16:24:34.958154'),('nmn186rq05hox7xkmp70eznyu2caizm6','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMU9s:CK4h_PVmUsWDLFKRWgHq8gUE_8pmN3sA7VWXF1JB8YY','2026-11-12 17:43:24.568958'),('oalp053fmax3zy2p4h1mbq71st5k28x3','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUqg:WtzpVZ29AIxjMRiVHVkuUpwLEnsE5gc8PH1FCF2v_Pg','2026-11-12 18:27:38.250059'),('obwuk13jkab99yhib9378kch95ic558c','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUpT:HYeHa-2etcu5EMWW_mbeyH3W00eDFOaMG5wyBd91n7M','2026-11-12 18:26:23.619571'),('olrl1xsdwkr9lrurc7yjxn99cwsjtb5q','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUqG:jwf7mH1GSTMve5c6Y70Onrr6l9jykzx9Li0WN-oH7ho','2026-11-12 18:27:12.594764'),('omlarnf7eoxbtetgwq8jrmtjqbqc75lq','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUtL:jTLiHmkb8T6tuMIwmACXxVsUl0z5UTO2-K3nbDAEG10','2026-11-12 18:30:23.282285'),('ov5ramzmgqs4ez21kzm1fbc26h3u85l4','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUkQ:Ecs3G-1oUepc4IE3f9pA3xzfRaITSKEVRTF5N-bCxIA','2026-11-12 18:21:10.701965'),('p63x0qv5yp4lqm4ig8bywnabfbh9mlz3','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMSOH:23Gm0v-e3_mwsIKzB9r5Hd4gNde0YrXw6CK9YDsi704','2026-11-12 15:50:09.188247'),('pc3wharmtvkssk6yljbjn704hvlnadi0','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMSp6:K-UB7_91mB4wpnWxYGl5GJAqvOiCA_R7Kb7c4IQUZwE','2026-11-12 16:17:52.551835'),('pcp5fh2ll1ktq944maucvvrc69x08id9','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUqq:_Bul1Hetn8M3XIX_zghLcs3gaiaNSzmTgkXsUCWjMhI','2026-11-12 18:27:48.020457'),('ph7pcueikohykqb32ob0v7vcdrow62g0','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUpD:p8PfeW-kenQiolRNwziMLuyTMqAcMNvyljzsQwu1edM','2026-11-12 18:26:07.634990'),('pjqa2a8tte54l3gms0072s5xust0h81f','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUAN:mhcPg5Nmea_QVH38q6TP43-Q2JU6WaGffldTY-XAAWI','2026-11-12 17:43:55.779348'),('pjqannljsdamsn1x8nwhnp9zqhu63ar4','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUr0:o8nKkLZOYiWizsAixjTQnkFrujspkAcIxeRHnJkjhBg','2026-11-12 18:27:58.015645'),('pmxiytghbl8at4ozsf1uf7ttet1cbyyt','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUpp:Qa6LvKvWS9BDBB2M414mRM_rNqVwBKA_TGizcLErG1I','2026-11-12 18:26:45.530966'),('q1daaixicemvl0gsiwwup3nvmosvd5g9','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUmK:MAZeQWeeb5aoTsvtE_92RpzkVBZyIuFq2lTBEEsoxyY','2026-11-12 18:23:08.761982'),('qdtc439shwzqa0284mf5e5e7amofh500','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUoQ:gYc6hTsNrgYr9ciH89uU_q_E-g6PE0jLyqZwM-izeXI','2026-11-12 18:25:18.370657'),('qnhxy5i5abjt7ie9k9ckcb94z8mgud4w','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUrH:3mSAQHiOjOf37_e7ZsM4flCtJwlea7KVBozGE1dWLLY','2026-11-12 18:28:15.991565'),('qv3t9p2o4j7e1r0zrnsbsmbklmkvl0d0','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMU8x:Alkbae-EesSGbozTB2iV7Z8dxo6QYDnz9KuZwyIwtWM','2026-11-12 17:42:27.713601'),('r6bkznol3sh7owe8auby81jw39862p85','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUrH:p7qdDAgDGIP_0Ulzk1fq5c22--YeHFg9UzC1F0mVCgw','2026-11-12 18:28:15.574748'),('rrtgljt22uq03wul421to5r2rmrfp4i5','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUr0:pz7zf8AffuPZ9B1-4Ivpx-MBTAh7MPLbjYnPsopPID0','2026-11-12 18:27:58.480543'),('rs0ojn8wogj1d6pegw5hxkueyrkdt0mo','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUqq:ddU_vy25kQKXBiT20xPkyQ7tjeWZXsEcP0DeY4-ZPGA','2026-11-12 18:27:48.240284'),('s1xum36hwue1vczllkbwywxzlkiy3mrf','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMTRU:NPYBUCJV_9RBB3x-3sirdXAs1Q0JhlRuXALaj2GU7jI','2026-11-12 16:57:32.528483'),('s9ijyxn6cbc5pu5q3azdwax93dmrpq39','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUqq:AMlmA2bAGKZuhX57GQl60EGO1hSvNBFehiCCjaLy4rE','2026-11-12 18:27:48.663987'),('siyrm14u72hfqpqpqmz6r8sofigsmzv1','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUoQ:lGyV_BCe-v-WCLBgZF24Ecq7IwpTgQgkFByU0TytAKM','2026-11-12 18:25:18.817651'),('t0huwip1tjirm6gbnmu3ttzgy46k20zp','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMSp3:R6mI-ZlG_cS8dj4AkgNAaofBdE_QUZmHCeU-GWIv-k4','2026-11-12 16:17:49.829877'),('tiypubd9u7ulmp41883kw6e0bhp4d03h','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMSw8:Rby9aZyDQTNLE2gcSoHFMhKtdUWQsOyR5EoxXZLqsv4','2026-11-12 16:25:08.865397'),('u6ev58uxde4t9ez23qyxfaltbbpbftft','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUjH:UK6Y27CmPoHsbmyi_90Dw2AxNXQ13hI8U7QhqPf2rmc','2026-11-12 18:19:59.103906'),('ugdz4cc4vmubpevibwk6cm2apyaan3wo','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMSxb:rLC7zWvRU1mcN4qr2wt_bxViyXyWDZRy-1DI69vHde8','2026-11-12 16:26:39.221969'),('un6qe012o5lm05jbp0w3syd2bmlnb3sw','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUdQ:li3NL1oVyxTuvDOrU_JBd6CNRHuSFsru4d_QxNqUnLM','2026-11-12 18:13:56.148767'),('v2zhu34sqfvrj2vz7ei0kpvq4whgb1qf','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUhz:MV9UAaHjB2ZG1---OB8WeCzqSdnQ2wSq54LxMQsoTrQ','2026-11-12 18:18:39.389145'),('v7ftfljw8xzhypc06ro7t3301t13qv0f','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUpD:SzCTkWQWWJurL94oDQ3SOHsYHBMnTGpOWxEr8TtVXio','2026-11-12 18:26:07.399169'),('vbmdu3lszm80l07k9ly1dhr0paj5n92n','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUpT:N39WqHjUfOKw9nb4iS2awh6BSazsH0flOSudkeUEO2A','2026-11-12 18:26:23.826308'),('vgw772vlr5bwpi8xppazbneqtszx3u68','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUdP:a2eLieid0vGvlt3BeXqYqym5oLr1XcnaaUU3R7fnzac','2026-11-12 18:13:55.924460'),('vvlikel9kdg9lqlh3918ech4yyvs0uz3','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMTRS:IpoxUZvg81XwZVeozY-x0zQfOSnhl7wucGun7f_Q1Yg','2026-11-12 16:57:30.739194'),('vx53p0b2g9zovu69iiij5up3425jtbg9','.eJxVjDsOwjAQBe_iGln-xJ9Q0ucM1np3wQFkS3FSIe4OkVJA-2bmvUSCbS1p67ykmcRZOHH63TLgg-sO6A711iS2ui5zlrsiD9rl1Iifl8P9OyjQy7dGxdrawYcAgXy0mjCPHhmj02CuThuKcSCrRrZsIaIOkQEHFwwBkxLvD908OAU:1xMUtM:Yf3gqrfwDbKgjlOTpg2QGbdIUKI502G6GudcwetQVx0','2026-11-12 18:30:24.273729'),('w43hw5xc34rji6z251aa6qxrpe5yijth','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMSyt:l3Sogtxl0DvF6EdOLMgAnf26jdT1YviEM2ZHLqQC5YU','2026-11-12 16:27:59.337495'),('wario0qa263206gck7snm599h7mtzjk7','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSys:RiadFvrIyBzGyajEN-So1W4TVSfW5HdcVuE26GmCNy8','2026-11-12 16:27:58.692074'),('wfrdw57jm9e1zl1px741bzted58akj7x','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUkR:pRpnbG8YD7UC-uyiGCbicBXlgmtoHKLK1ilGvfgFJWk','2026-11-12 18:21:11.217849'),('whd67gx36q1r6wo05x88vsobmuqp6laj','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMUoQ:cT5dYWtqL3FFAXyI6g07nB4JYpFEsZSNg0BAHpudPHc','2026-11-12 18:25:18.596138'),('wimnr6hmjup7mak4orf59gnzlk1l233h','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUqG:CNw89UQRiN0xmTJW3wxDkn88AMAmZvEp8vBX3cJFMVM','2026-11-12 18:27:12.823010'),('wjnongslzfzlzi99in7t8em2c9r8in4g','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUOh:xtuuCRXINbsM4_IGPn7jpxuJ9FqzdAfZuENpBWIK2To','2026-11-12 17:58:43.800314'),('wjpx08nmk5jwy2d3ngopbey4er8hk2n2','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUtN:aUCw7V-_mVmXnOJ4S03n93vxvPz-V0lGh4buLDrqGQU','2026-11-12 18:30:25.834989'),('yc3bpdt608lppdg9uq3yeshy55k8pa01','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMSwA:CyRXWf3V839xx5JOhr3eNgADHjyotyaI4lYD8SmKxVY','2026-11-12 16:25:10.289695'),('ynk6gu57v83km683xmg45ty16uc0t2vj','.eJxVjEEOwiAQAP_C2RCQpYBH776BLLtUqgaS0p6MfzckPeh1ZjJvEXHfStx7XuPC4iJAnH5ZQnrmOgQ_sN6bpFa3dUlyJPKwXd4a59f1aP8GBXsZWyZnwAVQATKcDWAKqIl80hrNlLzSYIK1CWawmthZPSuTmV2eLJIXny_Gdzdu:1xMSNh:wzHEUY1V2VFAPipLrZShslmmujZGSCbRtfdFyUtYCbs','2026-11-12 15:49:33.087894'),('z1gam86q4is6fabanos47v6a0qyl5tfx','.eJxVjEEOwiAQRe_C2pAyVBhcuu8ZyDADUjVtUtqV8e7apAvd_vfef6lI21rj1vISR1EXZdTpd0vEjzztQO403WbN87QuY9K7og_a9DBLfl4P9--gUqvfGsAE8KWAsJfAwJixR-wNeeis9FhcAo_-TBmQknWOMVixiQ0Yazv1_gDPJzca:1xMUqH:YYH1nVXQP95v2xAOGI_7tTLu8s-7gHIhLqNOJXg6xH0','2026-11-12 18:27:13.434803'),('z3tcvg4j7tuiy2o99obtbfh9ym2771h6','.eJxVjEEOgjAQRe_StWmGttji0j1naGamM4IaSCisjHdXEha6_e-9_zIZt3XIW5Ulj8VcjDOn342QHzLtoNxxus2W52ldRrK7Yg9abT8XeV4P9-9gwDp86zN1oCWoDwk4eWgawAZFYqLkNEmgVlAV1EPnmB3FEKMCt068Kifz_gDpZjhg:1xMUpC:7Q2GqPV62y0HFpfBNyvjEqNqmbUbdbPGqjLYQ2WTA0g','2026-11-12 18:26:06.928013'),('z4502z6qjajum5cxxosjxgicgfak3tof','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUNa:koj3r6KAytLmqFpnfaqi5c2-04tsrNOUQh3WdYSNkZ8','2026-11-12 17:57:34.337766'),('z6m8rlfxy0f1ci8bl2af63fv57c4tugn','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMUo3:t89Cle0__Hr3yK-s5-aIdYi3rofh5Gqip073yA8sP6g','2026-11-12 18:24:55.807756'),('zkqrh60o8f10jy4qcuvxba0af7ffpkud','.eJxVjE0OwiAYBe_C2pACpQGX7j0D-f6QqoGktCvj3bVJF7p9M_NeKsG2lrR1WdLM6qycOv1uCPSQugO-Q701Ta2uy4x6V_RBu742luflcP8OCvTyrWnIVkB4ILQmZ-tG8hgkTCMQQTTRBWCLbhA27DACxhDyFLMfvRNG9f4AGEg5IA:1xMSvb:qLaynn_k35AEy7bxDzSj0_IoYAYzIIlYdf3gt9xKnzY','2026-11-12 16:24:35.681566');
/*!40000 ALTER TABLE `django_session` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `finance_record`
--

DROP TABLE IF EXISTS `finance_record`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `finance_record` (
  `id` int NOT NULL AUTO_INCREMENT,
  `类型` varchar(10) NOT NULL,
  `金额` varchar(45) NOT NULL,
  `事由` varchar(100) NOT NULL,
  `关联订单` varchar(45) DEFAULT NULL,
  `日期` varchar(45) NOT NULL,
  `经手人` varchar(45) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=10 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `finance_record`
--

LOCK TABLES `finance_record` WRITE;
/*!40000 ALTER TABLE `finance_record` DISABLE KEYS */;
INSERT INTO `finance_record` VALUES (2,'支出','120.50','电梯维修',NULL,'2026-10-30','finance'),(3,'支出','80','清洁物耗材',NULL,'2026-10-30','finance'),(5,'支出','150.50','采购支出',NULL,'2026-11-02','finance'),(6,'收入','188','房费过租（订单103）','103','2026-10-30','manager'),(7,'收入','268','客房结算（订单104）','104','2026-10-30','front'),(8,'收入','188','房费过租（订单103）','103','2026-10-30','admin'),(9,'收入','268.00','客房结算（订单105）','105','2026-10-30','front');
/*!40000 ALTER TABLE `finance_record` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `folio`
--

DROP TABLE IF EXISTS `folio`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `folio` (
  `id` int NOT NULL AUTO_INCREMENT,
  `类型` varchar(10) NOT NULL,
  `金额` varchar(45) NOT NULL,
  `事由` varchar(100) NOT NULL,
  `日期` varchar(45) NOT NULL,
  `经手人` varchar(45) DEFAULT NULL,
  `order_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `folio_order_id_a8a60684_fk` (`order_id`),
  CONSTRAINT `folio_order_id_a8a60684_fk` FOREIGN KEY (`order_id`) REFERENCES `accommodation` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `folio`
--

LOCK TABLES `folio` WRITE;
/*!40000 ALTER TABLE `folio` DISABLE KEYS */;
INSERT INTO `folio` VALUES (1,'押金','300','散客直入押金','2026-10-30','front',105),(2,'房费','268','续住加收一晚房费','2026-10-30','front',105),(3,'收款','268.00','快捷离店自动结算','2026-10-30','front',105);
/*!40000 ALTER TABLE `folio` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `food`
--

DROP TABLE IF EXISTS `food`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `food` (
  `菜品编号` int NOT NULL,
  `菜名` varchar(45) NOT NULL,
  `价格` varchar(45) NOT NULL,
  `厨师id` int NOT NULL,
  PRIMARY KEY (`菜品编号`),
  UNIQUE KEY `food_菜名_609e9c52_uniq` (`菜名`),
  KEY `food_厨师id_1eda2d69_fk_staff_员工id` (`厨师id`),
  CONSTRAINT `food_厨师id_1eda2d69_fk_staff_员工id` FOREIGN KEY (`厨师id`) REFERENCES `staff` (`员工id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `food`
--

LOCK TABLES `food` WRITE;
/*!40000 ALTER TABLE `food` DISABLE KEYS */;
INSERT INTO `food` VALUES (1,'红烧肉','38',101),(10,'宫保鸡丁','38',102),(11,'清蒸鲈鱼','68',102),(12,'麻婆豆腐','22',102),(30,'回归菜品','55',102),(2297,'回归菜品297918','48',102),(2298,'回归菜品298070','48',102),(2343,'432','43',102),(2979,'回归菜品297998','48',102),(2981,'回归菜品二九八一八八','48',102),(2982,'回归菜品二九八二九五','48',102),(2983,'回归菜品二九八三一七','48',102),(2984,'回归菜品二九八四零四','48',102);
/*!40000 ALTER TABLE `food` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `guests_guest`
--

DROP TABLE IF EXISTS `guests_guest`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `guests_guest` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `full_name` varchar(80) NOT NULL,
  `phone` varchar(24) NOT NULL,
  `phone_normalized` varchar(24) DEFAULT NULL,
  `email` varchar(254) NOT NULL,
  `notes` longtext NOT NULL,
  `consent_version` varchar(32) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `property_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_guest_property_phone` (`property_id`,`phone_normalized`),
  KEY `idx_guest_prop_name` (`property_id`,`full_name`),
  CONSTRAINT `guests_guest_property_id_4564eef0_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `guests_guest`
--

LOCK TABLES `guests_guest` WRITE;
/*!40000 ALTER TABLE `guests_guest` DISABLE KEYS */;
/*!40000 ALTER TABLE `guests_guest` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `inventory_dailyrate`
--

DROP TABLE IF EXISTS `inventory_dailyrate`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `inventory_dailyrate` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `stay_date` date NOT NULL,
  `price` decimal(12,2) NOT NULL,
  `currency` varchar(3) NOT NULL,
  `closed` tinyint(1) NOT NULL,
  `min_stay` smallint unsigned NOT NULL,
  `max_stay` smallint unsigned DEFAULT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `property_id` bigint NOT NULL,
  `rate_plan_id` bigint NOT NULL,
  `room_type_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_daily_rate_plan_room_date` (`rate_plan_id`,`room_type_id`,`stay_date`),
  KEY `idx_rate_prop_type_date` (`property_id`,`room_type_id`,`stay_date`),
  KEY `inventory_dailyrate_room_type_id_350cf181_fk_inventory` (`room_type_id`),
  CONSTRAINT `inventory_dailyrate_property_id_76df9634_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `inventory_dailyrate_rate_plan_id_de07c86d_fk_inventory` FOREIGN KEY (`rate_plan_id`) REFERENCES `inventory_rateplan` (`id`),
  CONSTRAINT `inventory_dailyrate_room_type_id_350cf181_fk_inventory` FOREIGN KEY (`room_type_id`) REFERENCES `inventory_roomtype` (`id`),
  CONSTRAINT `ck_daily_rate_nonnegative` CHECK ((`price` >= 0)),
  CONSTRAINT `inventory_dailyrate_chk_1` CHECK ((`min_stay` >= 0)),
  CONSTRAINT `inventory_dailyrate_chk_2` CHECK ((`max_stay` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `inventory_dailyrate`
--

LOCK TABLES `inventory_dailyrate` WRITE;
/*!40000 ALTER TABLE `inventory_dailyrate` DISABLE KEYS */;
/*!40000 ALTER TABLE `inventory_dailyrate` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `inventory_rateplan`
--

DROP TABLE IF EXISTS `inventory_rateplan`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `inventory_rateplan` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `code` varchar(32) NOT NULL,
  `name` varchar(80) NOT NULL,
  `valid_from` date NOT NULL,
  `valid_to` date DEFAULT NULL,
  `cancellation_policy` longtext NOT NULL,
  `deposit_policy` longtext NOT NULL,
  `is_active` tinyint(1) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `property_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rate_plan_property_code` (`property_id`,`code`),
  CONSTRAINT `inventory_rateplan_property_id_829ebeb9_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `ck_rate_plan_valid_dates` CHECK (((`valid_to` is null) or (`valid_to` >= `valid_from`)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `inventory_rateplan`
--

LOCK TABLES `inventory_rateplan` WRITE;
/*!40000 ALTER TABLE `inventory_rateplan` DISABLE KEYS */;
/*!40000 ALTER TABLE `inventory_rateplan` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `inventory_room`
--

DROP TABLE IF EXISTS `inventory_room`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `inventory_room` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `number` varchar(16) NOT NULL,
  `floor` varchar(16) NOT NULL,
  `occupancy_status` varchar(16) NOT NULL,
  `housekeeping_status` varchar(16) NOT NULL,
  `maintenance_status` varchar(20) NOT NULL,
  `is_active` tinyint(1) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `property_id` bigint NOT NULL,
  `room_type_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_room_property_number` (`property_id`,`number`),
  KEY `idx_room_prop_occ_clean` (`property_id`,`occupancy_status`,`housekeeping_status`),
  KEY `inventory_room_room_type_id_de056152_fk_inventory_roomtype_id` (`room_type_id`),
  CONSTRAINT `inventory_room_property_id_4772c0ca_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `inventory_room_room_type_id_de056152_fk_inventory_roomtype_id` FOREIGN KEY (`room_type_id`) REFERENCES `inventory_roomtype` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `inventory_room`
--

LOCK TABLES `inventory_room` WRITE;
/*!40000 ALTER TABLE `inventory_room` DISABLE KEYS */;
/*!40000 ALTER TABLE `inventory_room` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `inventory_roomtype`
--

DROP TABLE IF EXISTS `inventory_roomtype`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `inventory_roomtype` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `code` varchar(32) NOT NULL,
  `name` varchar(80) NOT NULL,
  `capacity` smallint unsigned NOT NULL,
  `bed_type` varchar(40) NOT NULL,
  `amenities` json NOT NULL,
  `is_active` tinyint(1) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `property_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_room_type_property_code` (`property_id`,`code`),
  CONSTRAINT `inventory_roomtype_property_id_7c1a744d_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `ck_room_type_capacity_positive` CHECK ((`capacity` >= 1)),
  CONSTRAINT `inventory_roomtype_chk_1` CHECK ((`capacity` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `inventory_roomtype`
--

LOCK TABLES `inventory_roomtype` WRITE;
/*!40000 ALTER TABLE `inventory_roomtype` DISABLE KEYS */;
/*!40000 ALTER TABLE `inventory_roomtype` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `inventory_roomtypeinventory`
--

DROP TABLE IF EXISTS `inventory_roomtypeinventory`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `inventory_roomtypeinventory` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `stay_date` date NOT NULL,
  `total_rooms` int unsigned NOT NULL,
  `held` int unsigned NOT NULL,
  `sold` int unsigned NOT NULL,
  `out_of_service` int unsigned NOT NULL,
  `stop_sell` tinyint(1) NOT NULL,
  `version` int unsigned NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `property_id` bigint NOT NULL,
  `room_type_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_inventory_property_type_date` (`property_id`,`room_type_id`,`stay_date`),
  KEY `idx_inv_prop_type_date` (`property_id`,`room_type_id`,`stay_date`),
  KEY `inventory_roomtypein_room_type_id_73a36496_fk_inventory` (`room_type_id`),
  CONSTRAINT `inventory_roomtypein_property_id_7329e7f1_fk_core_prop` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `inventory_roomtypein_room_type_id_73a36496_fk_inventory` FOREIGN KEY (`room_type_id`) REFERENCES `inventory_roomtype` (`id`),
  CONSTRAINT `ck_inventory_capacity_not_exceeded` CHECK ((`total_rooms` >= ((`held` + `sold`) + `out_of_service`))),
  CONSTRAINT `inventory_roomtypeinventory_chk_1` CHECK ((`total_rooms` >= 0)),
  CONSTRAINT `inventory_roomtypeinventory_chk_2` CHECK ((`held` >= 0)),
  CONSTRAINT `inventory_roomtypeinventory_chk_3` CHECK ((`sold` >= 0)),
  CONSTRAINT `inventory_roomtypeinventory_chk_4` CHECK ((`out_of_service` >= 0)),
  CONSTRAINT `inventory_roomtypeinventory_chk_5` CHECK ((`version` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `inventory_roomtypeinventory`
--

LOCK TABLES `inventory_roomtypeinventory` WRITE;
/*!40000 ALTER TABLE `inventory_roomtypeinventory` DISABLE KEYS */;
/*!40000 ALTER TABLE `inventory_roomtypeinventory` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ledger_entry`
--

DROP TABLE IF EXISTS `ledger_entry`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `ledger_entry` (
  `id` int NOT NULL AUTO_INCREMENT,
  `entry_type` varchar(10) NOT NULL,
  `amount` decimal(12,2) NOT NULL,
  `description` varchar(200) NOT NULL,
  `business_date` date NOT NULL,
  `operator` varchar(150) DEFAULT NULL,
  `source_type` varchar(20) NOT NULL,
  `source_id` varchar(64) DEFAULT NULL,
  `reference` varchar(120) DEFAULT NULL,
  `is_void` tinyint(1) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `accommodation_id` int DEFAULT NULL,
  `reservation_id` int DEFAULT NULL,
  `reverses_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `reference` (`reference`),
  KEY `ledger_entry_reverses_id_e0cdc015_fk_ledger_entry_id` (`reverses_id`),
  KEY `ledger_entr_accommo_e2c4bf_idx` (`accommodation_id`,`business_date`),
  KEY `ledger_entr_reserva_7d5a39_idx` (`reservation_id`,`business_date`),
  KEY `ledger_entry_entry_type_d3a5b505` (`entry_type`),
  KEY `ledger_entry_business_date_b3c6a9eb` (`business_date`),
  KEY `ledger_entry_source_type_67bcf0a8` (`source_type`),
  KEY `ledger_entry_source_id_3babe37e` (`source_id`),
  KEY `ledger_entry_is_void_e4d5d494` (`is_void`),
  CONSTRAINT `ledger_entry_accommodation_id_c704f47b_fk_accommodation_id` FOREIGN KEY (`accommodation_id`) REFERENCES `accommodation` (`id`),
  CONSTRAINT `ledger_entry_reservation_id_e0b8b86e_fk_reservation_id` FOREIGN KEY (`reservation_id`) REFERENCES `reservation` (`id`),
  CONSTRAINT `ledger_entry_reverses_id_e0cdc015_fk_ledger_entry_id` FOREIGN KEY (`reverses_id`) REFERENCES `ledger_entry` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=11 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ledger_entry`
--

LOCK TABLES `ledger_entry` WRITE;
/*!40000 ALTER TABLE `ledger_entry` DISABLE KEYS */;
INSERT INTO `ledger_entry` VALUES (1,'押金',300.00,'散客直入押金','2026-10-30','front','legacy_folio','105','legacy:folio:1',0,'2026-10-30 07:56:51.441823',105,NULL,NULL),(2,'房费',268.00,'续住加收一晚房费','2026-10-30','front','legacy_folio','105','legacy:folio:2',0,'2026-10-30 07:56:51.443920',105,NULL,NULL),(3,'收款',268.00,'快捷离店自动结算','2026-10-30','front','legacy_folio','105','legacy:folio:3',0,'2026-10-30 07:56:51.445924',105,NULL,NULL),(4,'支出',120.50,'电梯维修','2026-10-30','finance','legacy_finance','2','legacy:finance:2',0,'2026-10-30 07:56:51.449200',NULL,NULL,NULL),(5,'支出',80.00,'清洁物耗材','2026-10-30','finance','legacy_finance','3','legacy:finance:3',0,'2026-10-30 07:56:51.451214',NULL,NULL,NULL),(6,'支出',150.50,'采购支出','2026-11-02','finance','legacy_finance','5','legacy:finance:5',0,'2026-10-30 07:56:51.454491',NULL,NULL,NULL),(7,'收入',188.00,'房费过租（订单103）（关联订单 103）','2026-10-30','manager','legacy_finance','6','legacy:finance:6',0,'2026-10-30 07:56:51.455996',NULL,NULL,NULL),(8,'收入',268.00,'客房结算（订单104）（关联订单 104）','2026-10-30','front','legacy_finance','7','legacy:finance:7',0,'2026-10-30 07:56:51.458011',NULL,NULL,NULL),(9,'收入',188.00,'房费过租（订单103）（关联订单 103）','2026-10-30','admin','legacy_finance','8','legacy:finance:8',0,'2026-10-30 07:56:51.459010',NULL,NULL,NULL),(10,'收入',268.00,'客房结算（订单105）（关联订单 105）','2026-10-30','front','legacy_finance','9','legacy:finance:9',0,'2026-10-30 07:56:51.461011',NULL,NULL,NULL);
/*!40000 ALTER TABLE `ledger_entry` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `night_audit`
--

DROP TABLE IF EXISTS `night_audit`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `night_audit` (
  `id` int NOT NULL AUTO_INCREMENT,
  `营业日期` varchar(45) DEFAULT NULL,
  `在住单数` int NOT NULL,
  `过租总额` varchar(45) DEFAULT NULL,
  `未到处理数` int NOT NULL,
  `操作人` varchar(45) NOT NULL,
  `执行时间` varchar(45) NOT NULL,
  `营业日` date DEFAULT NULL,
  `过租金额` decimal(12,2) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `营业日` (`营业日`)
) ENGINE=InnoDB AUTO_INCREMENT=16 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `night_audit`
--

LOCK TABLES `night_audit` WRITE;
/*!40000 ALTER TABLE `night_audit` DISABLE KEYS */;
INSERT INTO `night_audit` VALUES (1,'2026-10-30',1,'188.00',0,'manager','2026-10-30 01:43','2026-10-30',188.00),(2,'2026-10-30',1,'188.00',0,'admin','2026-10-30 01:45',NULL,188.00),(3,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:14',NULL,0.00),(4,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:16',NULL,0.00),(5,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:18',NULL,0.00),(6,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:20',NULL,0.00),(7,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:21',NULL,0.00),(8,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:23',NULL,0.00),(9,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:25',NULL,0.00),(10,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:25',NULL,0.00),(11,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:26',NULL,0.00),(12,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:26',NULL,0.00),(13,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:26',NULL,0.00),(14,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:27',NULL,0.00),(15,'2026-10-30',0,'0.00',0,'manager','2026-10-30 02:28',NULL,0.00);
/*!40000 ALTER TABLE `night_audit` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `order`
--

DROP TABLE IF EXISTS `order`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `order` (
  `桌号` int NOT NULL,
  `id` int NOT NULL AUTO_INCREMENT,
  `服务员编号` int NOT NULL,
  `菜品编号` int NOT NULL,
  `顾客编号` int NOT NULL,
  `冲账人` varchar(45) DEFAULT NULL,
  `冲账时间` varchar(45) DEFAULT NULL,
  `状态` varchar(10) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `order_服务员编号_0debb5b8_fk_staff_员工id` (`服务员编号`),
  KEY `order_菜品编号_6da89f1d_fk_food_菜品编号` (`菜品编号`),
  KEY `order_顾客编号_45bccbd4_fk_client_顾客id` (`顾客编号`),
  CONSTRAINT `order_服务员编号_0debb5b8_fk_staff_员工id` FOREIGN KEY (`服务员编号`) REFERENCES `staff` (`员工id`),
  CONSTRAINT `order_菜品编号_6da89f1d_fk_food_菜品编号` FOREIGN KEY (`菜品编号`) REFERENCES `food` (`菜品编号`),
  CONSTRAINT `order_顾客编号_45bccbd4_fk_client_顾客id` FOREIGN KEY (`顾客编号`) REFERENCES `client` (`顾客id`)
) ENGINE=InnoDB AUTO_INCREMENT=9299 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `order`
--

LOCK TABLES `order` WRITE;
/*!40000 ALTER TABLE `order` DISABLE KEYS */;
INSERT INTO `order` VALUES (1,101,101,1,1,NULL,NULL,'正常'),(3,301,11111,1,1,'manager','2026-10-30','已冲账'),(8,401,103,10,1,NULL,NULL,'正常'),(8,4297,103,10,1,NULL,NULL,'正常'),(8,4298,103,10,1,NULL,NULL,'正常'),(8,9298,103,10,1,NULL,NULL,'正常');
/*!40000 ALTER TABLE `order` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `park`
--

DROP TABLE IF EXISTS `park`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `park` (
  `车位编号` int NOT NULL,
  `当前状态` varchar(45) NOT NULL,
  `车辆牌号` varchar(45) DEFAULT NULL,
  PRIMARY KEY (`车位编号`),
  UNIQUE KEY `park_车辆牌号_012b9e43_uniq` (`车辆牌号`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `park`
--

LOCK TABLES `park` WRITE;
/*!40000 ALTER TABLE `park` DISABLE KEYS */;
INSERT INTO `park` VALUES (1,'空闲','黑A12345'),(2,'有车','京B67890'),(3,'空闲',NULL),(10,'有车','沪C12345'),(20,'有车','京A88888'),(222,'空闲','黑A123451'),(333,'有车','黑A123222'),(1297,'有车','沪C297918'),(1298,'有车','沪C298070'),(1979,'有车','沪C297998'),(1981,'有车','沪C298188'),(1982,'有车','沪C298295'),(1983,'有车','沪C298317'),(1984,'有车','沪C298404');
/*!40000 ALTER TABLE `park` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `payment`
--

DROP TABLE IF EXISTS `payment`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `payment` (
  `id` int NOT NULL AUTO_INCREMENT,
  `provider` varchar(30) NOT NULL,
  `provider_ref` varchar(100) NOT NULL,
  `amount` decimal(12,2) NOT NULL,
  `status` varchar(10) NOT NULL,
  `raw_payload` json NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `paid_at` datetime(6) DEFAULT NULL,
  `reservation_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `provider_ref` (`provider_ref`),
  KEY `payment_reservation_id_217d2e5e_fk_reservation_id` (`reservation_id`),
  KEY `payment_status_f6b79473` (`status`),
  CONSTRAINT `payment_reservation_id_217d2e5e_fk_reservation_id` FOREIGN KEY (`reservation_id`) REFERENCES `reservation` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `payment`
--

LOCK TABLES `payment` WRITE;
/*!40000 ALTER TABLE `payment` DISABLE KEYS */;
/*!40000 ALTER TABLE `payment` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `reservation`
--

DROP TABLE IF EXISTS `reservation`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reservation` (
  `id` int NOT NULL AUTO_INCREMENT,
  `booking_no` varchar(32) NOT NULL,
  `room_type` varchar(45) NOT NULL,
  `check_in` date NOT NULL,
  `check_out` date NOT NULL,
  `rooms` smallint unsigned NOT NULL,
  `status` varchar(12) NOT NULL,
  `total_amount` decimal(12,2) NOT NULL,
  `deposit_amount` decimal(12,2) NOT NULL,
  `expires_at` datetime(6) NOT NULL,
  `idempotency_key` varchar(100) DEFAULT NULL,
  `source` varchar(20) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `guest_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `booking_no` (`booking_no`),
  UNIQUE KEY `idempotency_key` (`idempotency_key`),
  KEY `reservation_guest_id_fb505a01_fk_client_顾客id` (`guest_id`),
  KEY `reservation_room_ty_68b1e8_idx` (`room_type`,`check_in`,`check_out`),
  KEY `reservation_room_type_e16bb555` (`room_type`),
  KEY `reservation_check_in_1357afdb` (`check_in`),
  KEY `reservation_check_out_907b26ff` (`check_out`),
  KEY `reservation_status_9f1adbd4` (`status`),
  KEY `reservation_expires_at_784781dc` (`expires_at`),
  CONSTRAINT `reservation_guest_id_fb505a01_fk_client_顾客id` FOREIGN KEY (`guest_id`) REFERENCES `client` (`顾客id`),
  CONSTRAINT `reservation_chk_1` CHECK ((`rooms` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `reservation`
--

LOCK TABLES `reservation` WRITE;
/*!40000 ALTER TABLE `reservation` DISABLE KEYS */;
/*!40000 ALTER TABLE `reservation` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `reservations_reservation`
--

DROP TABLE IF EXISTS `reservations_reservation`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reservations_reservation` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `booking_no` varchar(32) NOT NULL,
  `source` varchar(24) NOT NULL,
  `status` varchar(20) NOT NULL,
  `check_in` date NOT NULL,
  `check_out` date NOT NULL,
  `room_count` smallint unsigned NOT NULL,
  `adult_count` smallint unsigned NOT NULL,
  `child_count` smallint unsigned NOT NULL,
  `total_amount` decimal(12,2) NOT NULL,
  `deposit_amount` decimal(12,2) NOT NULL,
  `currency` varchar(3) NOT NULL,
  `idempotency_key` varchar(128) DEFAULT NULL,
  `request_fingerprint` varchar(64) NOT NULL,
  `expires_at` datetime(6) DEFAULT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `guest_id` bigint NOT NULL,
  `property_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `booking_no` (`booking_no`),
  UNIQUE KEY `uq_reservation_property_idempotency` (`property_id`,`idempotency_key`),
  KEY `idx_res_prop_status_date` (`property_id`,`status`,`check_in`),
  KEY `reservations_reservation_guest_id_c59ab314_fk_guests_guest_id` (`guest_id`),
  CONSTRAINT `reservations_reserva_property_id_be19c1de_fk_core_prop` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `reservations_reservation_guest_id_c59ab314_fk_guests_guest_id` FOREIGN KEY (`guest_id`) REFERENCES `guests_guest` (`id`),
  CONSTRAINT `ck_reservation_adults_positive` CHECK ((`adult_count` >= 1)),
  CONSTRAINT `ck_reservation_checkout_after_checkin` CHECK ((`check_out` > `check_in`)),
  CONSTRAINT `ck_reservation_rooms_positive` CHECK ((`room_count` >= 1)),
  CONSTRAINT `reservations_reservation_chk_1` CHECK ((`room_count` >= 0)),
  CONSTRAINT `reservations_reservation_chk_2` CHECK ((`adult_count` >= 0)),
  CONSTRAINT `reservations_reservation_chk_3` CHECK ((`child_count` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `reservations_reservation`
--

LOCK TABLES `reservations_reservation` WRITE;
/*!40000 ALTER TABLE `reservations_reservation` DISABLE KEYS */;
/*!40000 ALTER TABLE `reservations_reservation` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `reservations_reservationroom`
--

DROP TABLE IF EXISTS `reservations_reservationroom`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reservations_reservationroom` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `check_in` date NOT NULL,
  `check_out` date NOT NULL,
  `nightly_rates` json NOT NULL,
  `amount` decimal(12,2) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `assigned_room_id` bigint DEFAULT NULL,
  `property_id` bigint NOT NULL,
  `rate_plan_id` bigint NOT NULL,
  `reservation_id` bigint NOT NULL,
  `room_type_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  KEY `reservations_reserva_assigned_room_id_5013b5ac_fk_inventory` (`assigned_room_id`),
  KEY `reservations_reserva_property_id_b7ac61f7_fk_core_prop` (`property_id`),
  KEY `reservations_reserva_rate_plan_id_87ec79fa_fk_inventory` (`rate_plan_id`),
  KEY `reservations_reserva_reservation_id_a3a1e915_fk_reservati` (`reservation_id`),
  KEY `reservations_reserva_room_type_id_ff4f7e51_fk_inventory` (`room_type_id`),
  CONSTRAINT `reservations_reserva_assigned_room_id_5013b5ac_fk_inventory` FOREIGN KEY (`assigned_room_id`) REFERENCES `inventory_room` (`id`),
  CONSTRAINT `reservations_reserva_property_id_b7ac61f7_fk_core_prop` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `reservations_reserva_rate_plan_id_87ec79fa_fk_inventory` FOREIGN KEY (`rate_plan_id`) REFERENCES `inventory_rateplan` (`id`),
  CONSTRAINT `reservations_reserva_reservation_id_a3a1e915_fk_reservati` FOREIGN KEY (`reservation_id`) REFERENCES `reservations_reservation` (`id`),
  CONSTRAINT `reservations_reserva_room_type_id_ff4f7e51_fk_inventory` FOREIGN KEY (`room_type_id`) REFERENCES `inventory_roomtype` (`id`),
  CONSTRAINT `ck_reservation_room_amount_nonnegative` CHECK ((`amount` >= 0)),
  CONSTRAINT `ck_reservation_room_checkout_after_checkin` CHECK ((`check_out` > `check_in`))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `reservations_reservationroom`
--

LOCK TABLES `reservations_reservationroom` WRITE;
/*!40000 ALTER TABLE `reservations_reservationroom` DISABLE KEYS */;
/*!40000 ALTER TABLE `reservations_reservationroom` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `reservations_stay`
--

DROP TABLE IF EXISTS `reservations_stay`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reservations_stay` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `actual_check_in` datetime(6) DEFAULT NULL,
  `actual_check_out` datetime(6) DEFAULT NULL,
  `status` varchar(16) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `property_id` bigint NOT NULL,
  `reservation_room_id` bigint NOT NULL,
  `room_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `reservation_room_id` (`reservation_room_id`),
  KEY `idx_stay_prop_status` (`property_id`,`status`),
  KEY `reservations_stay_room_id_79c97eb2_fk_inventory_room_id` (`room_id`),
  CONSTRAINT `reservations_stay_property_id_ca9499eb_fk_core_property_id` FOREIGN KEY (`property_id`) REFERENCES `core_property` (`id`),
  CONSTRAINT `reservations_stay_reservation_room_id_db99f5c9_fk_reservati` FOREIGN KEY (`reservation_room_id`) REFERENCES `reservations_reservationroom` (`id`),
  CONSTRAINT `reservations_stay_room_id_79c97eb2_fk_inventory_room_id` FOREIGN KEY (`room_id`) REFERENCES `inventory_room` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `reservations_stay`
--

LOCK TABLES `reservations_stay` WRITE;
/*!40000 ALTER TABLE `reservations_stay` DISABLE KEYS */;
/*!40000 ALTER TABLE `reservations_stay` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `room`
--

DROP TABLE IF EXISTS `room`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `room` (
  `房间号` int NOT NULL,
  `客房类型` varchar(45) NOT NULL,
  `价格` int NOT NULL,
  `负责人编号` int NOT NULL,
  `房间状态` varchar(10) DEFAULT NULL,
  `占用状态` varchar(10) NOT NULL,
  `清洁状态` varchar(10) NOT NULL,
  `维修状态` varchar(10) NOT NULL,
  PRIMARY KEY (`房间号`),
  KEY `room_负责人编号_2dabc7af_fk_staff_员工id` (`负责人编号`),
  KEY `room_房间状态_1a5992b0` (`房间状态`),
  KEY `room_占用状态_d11370c7` (`占用状态`),
  KEY `room_清洁状态_e015cadc` (`清洁状态`),
  KEY `room_维修状态_97af55a3` (`维修状态`),
  CONSTRAINT `room_负责人编号_2dabc7af_fk_staff_员工id` FOREIGN KEY (`负责人编号`) REFERENCES `staff` (`员工id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `room`
--

LOCK TABLES `room` WRITE;
/*!40000 ALTER TABLE `room` DISABLE KEYS */;
INSERT INTO `room` VALUES (1,'标准双床房',188,11111,'已入住','已入住','干净','正常'),(201,'商务大床房',268,101,'已预订','已预订','干净','正常'),(202,'商务大床房',268,102,'已预订','已预订','干净','正常'),(203,'商务大床房',268,101,'空闲','空闲','干净','正常'),(301,'豪华双床房',328,101,'空闲','空闲','干净','正常'),(302,'豪华双床房',328,102,'空闲','空闲','干净','正常'),(401,'家庭亲子房',458,101,'空闲','空闲','干净','正常'),(402,'家庭亲子房',458,102,'空闲','空闲','干净','正常'),(501,'行政套房',688,101,'空闲','空闲','干净','正常'),(502,'行政套房',688,102,'空闲','空闲','干净','正常');
/*!40000 ALTER TABLE `room` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `room_type_inventory`
--

DROP TABLE IF EXISTS `room_type_inventory`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `room_type_inventory` (
  `id` int NOT NULL AUTO_INCREMENT,
  `room_type` varchar(45) NOT NULL,
  `stay_date` date NOT NULL,
  `total_rooms` int unsigned NOT NULL,
  `sold` int unsigned NOT NULL,
  `held` int unsigned NOT NULL,
  `stop_sell` tinyint(1) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_room_type_inventory_date` (`room_type`,`stay_date`),
  KEY `room_type_inventory_stay_date_8a8aa453` (`stay_date`),
  CONSTRAINT `room_type_inventory_chk_1` CHECK ((`total_rooms` >= 0)),
  CONSTRAINT `room_type_inventory_chk_2` CHECK ((`sold` >= 0)),
  CONSTRAINT `room_type_inventory_chk_3` CHECK ((`held` >= 0))
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `room_type_inventory`
--

LOCK TABLES `room_type_inventory` WRITE;
/*!40000 ALTER TABLE `room_type_inventory` DISABLE KEYS */;
INSERT INTO `room_type_inventory` VALUES (1,'标准双床房','2026-11-05',1,0,0,0,'2026-10-30 08:11:53.195945');
/*!40000 ALTER TABLE `room_type_inventory` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `staff`
--

DROP TABLE IF EXISTS `staff`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `staff` (
  `员工id` int NOT NULL AUTO_INCREMENT,
  `姓名` varchar(45) NOT NULL,
  `职务` varchar(45) NOT NULL,
  `月薪` varchar(45) NOT NULL,
  `工龄` varchar(45) NOT NULL,
  PRIMARY KEY (`员工id`)
) ENGINE=InnoDB AUTO_INCREMENT=111113 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `staff`
--

LOCK TABLES `staff` WRITE;
/*!40000 ALTER TABLE `staff` DISABLE KEYS */;
INSERT INTO `staff` VALUES (101,'王百万','总经理','12000','12'),(102,'张大厨','厨师','8000','5'),(103,'陈服务员','服务员','4500','2'),(2222,'莉莎克拉乐视','总经理','8000','2'),(3333,'莉莎克水水水水','大堂经理','8','2'),(11111,'莉莎','服务员','8000','1'),(111112,'OwnerTest','保安','5000','1');
/*!40000 ALTER TABLE `staff` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `vip`
--

DROP TABLE IF EXISTS `vip`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `vip` (
  `会员编号` int NOT NULL AUTO_INCREMENT,
  `姓名` varchar(45) NOT NULL,
  `会员等级` varchar(45) NOT NULL,
  `联系方式` varchar(45) NOT NULL,
  PRIMARY KEY (`会员编号`),
  UNIQUE KEY `vip_联系方式_95c17a0f_uniq` (`联系方式`)
) ENGINE=InnoDB AUTO_INCREMENT=11116 DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `vip`
--

LOCK TABLES `vip` WRITE;
/*!40000 ALTER TABLE `vip` DISABLE KEYS */;
INSERT INTO `vip` VALUES (1,'刘能','2','18312345678'),(2,'wwwww','1','1858532478524'),(11111,'莉莎克拉乐视','高级会员','17777777777'),(11112,'张会员','高级会员','13611112222'),(11113,'李会员','普通会员','13633334444'),(11114,'回归会员','普通会员','13877778888'),(11115,'回归会员','普通会员','13699998888');
/*!40000 ALTER TABLE `vip` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-10-30 23:49:37
