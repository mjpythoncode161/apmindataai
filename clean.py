import os
import sys
import django

# Setup Django
sys.path.append('D:/APMC/apmc')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'apmc.settings')
django.setup()

from accounts.models import *

for b in Bikri.objects.all():
    bags = BikriBagWeight.objects.filter(bikri=b).order_by('bag_no')
    diff = bags.count() - b.no_of_bags
    if diff > 0:
        extra = list(bags)[b.no_of_bags:]
        for e in extra:
            e.delete()
        print(f"Deleted {diff} extra bags for lot {b.avak.lot_number}")
