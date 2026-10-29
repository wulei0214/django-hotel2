"""hotel URL 路由表。

路由分组：
  /login/ /logout/          前台独立登录/退出
  /                         按当前角色权限跳转到首个可访问模块
  /check_unique/            唯一字段失焦查重接口（JSON）
  /index/ /insert/ ...      员工管理（index 即员工模块首页）
  /show_client/ ...         其余 7 个模块：show_/insert_/update_/delete_/search_ 五件套
  /admin/                   Django 后台（simpleui 主题，超管用）

所有业务路由均受登录 + 模块权限保护，见 hotelapp/views.py 各视图装饰器。
"""
"""hotel URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/2.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView
from hotelapp import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.home),
    path('home/', views.home),
    path('login/', views.login_view),
    path('register/', views.register_view),
    path('logout/', views.logout_view),
    path('booking/', views.booking_page),
    path('checkin/', views.checkin_manage),
    path('checkin_confirm/', views.checkin_confirm),
    path('checkout/', views.checkout),
    path('uncheckout/', views.uncheckout),
    path('room_status/', views.room_status_change),
    path('finance/', views.finance_manage),
    path('finance_add/', views.finance_add),
    path('check_unique/', views.check_unique),
    path('index/', views.index),
    path('insert/', views.insert),
    path('insert_client/', views.insert_client),
    path('delete_staff/', views.delete_staff),
    path('search_staff/', views.search_staff),
    #path('single/', views.single),
    path('update_staff/', views.update_staff),
    path('show_client/', views.show_client),
    path('delete_client/', views.delete_client),
    path('update_client/', views.update_client),
    path('search_client/', views.search_client),
    path('insert_food/', views.insert_food),
    path('show_food/', views.show_food),
    path('delete_food/', views.delete_food),
    path('update_food/', views.update_food),
    path('search_food/', views.search_food),
    path('insert_vip/', views.insert_vip),
    path('show_vip/', views.show_vip),
    path('delete_vip/', views.delete_vip),
    path('update_vip/', views.update_vip),
    path('search_vip/', views.search_vip),
    path('insert_park/', views.insert_park),
    path('show_park/', views.show_park),
    path('delete_park/', views.delete_park),
    path('update_park/', views.update_park),
    path('search_park/', views.search_park),
    #path('show_cook/', views.show_cook),
    path('insert_room/', views.insert_room),
    path('show_room/', views.show_room),
    path('delete_room/', views.delete_room),
    path('update_room/', views.update_room),
    path('search_room/', views.search_room),
    path('insert_order/', views.insert_order),
    path('show_order/', views.show_order),
    path('delete_order/', views.delete_order),
    path('search_order/', views.search_order),
    path('insert_accomodation/', views.insert_accomodation),
    path('delete_accomodation/', views.delete_accomodation),
    path('search_accomodation/', views.search_accomodation),
    path('show_accomodation/', views.show_accomodation),
]