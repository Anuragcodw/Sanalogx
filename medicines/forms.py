from django import forms
from django.core.exceptions import ValidationError
from .models import Medicine, MedicineBatch, Category

# Allowed file config
ALLOWED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp']
MAX_IMAGE_SIZE_MB = 5


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'code', 'description', 'notes', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Analgesics & Antipyretics'}),
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. ANALGESIC_ANTIPYRETIC'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Optional description'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Pharmacy review / clinical notes'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['code'].required = False
        self.fields['description'].required = False
        self.fields['notes'].required = False


class MedicineForm(forms.ModelForm):
    class Meta:
        model = Medicine
        fields = [
            'medicine_name', 'generic_name', 'brand_name', 'category',
            'dosage_form', 'strength', 'manufacturer', 'unit_of_measure',
            'regulatory_category',
            'tax_rate', 'mrp', 'max_discount_pct',
            'atc_code', 'storage_condition', 'source_reference', 'review_note',
            'description', 'barcode', 'qr_code', 'image',
            'reorder_level', 'minimum_stock', 'maximum_stock', 'safety_stock',
            'status'
        ]
        widgets = {
            'medicine_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. Paracetamol 500mg'
            }),
            'generic_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. Paracetamol'
            }),
            'brand_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. Panadol / Crocin (optional)'
            }),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'dosage_form': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. Tablet, Capsule, Syrup, Injection'
            }),
            'strength': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. 500 mg, 10 mg/mL'
            }),
            'manufacturer': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. GlaxoSmithKline, Sun Pharma'
            }),
            'unit_of_measure': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. Strip, Box, Bottle, Vial, Ampoule'
            }),
            'regulatory_category': forms.Select(attrs={'class': 'form-select'}),
            'tax_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'mrp': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'max_discount_pct': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '20.00'}),
            'atc_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. N02BE01'}),
            'storage_condition': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Store below 25°C'}),
            'source_reference': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Starter_Examples'}),
            'review_note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Clinical review criteria'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Clinical indications, therapeutic notes, and cautions...'}),
            'barcode': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Scan or enter barcode / GTIN'}),
            'qr_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. QR-PARACETAMOL-500'}),
            'image': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'reorder_level': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'minimum_stock': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'maximum_stock': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'safety_stock': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'status': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = Category.objects.filter(is_active=True)
        self.fields['brand_name'].required = False
        self.fields['barcode'].required = False
        self.fields['qr_code'].required = False
        self.fields['image'].required = False
        self.fields['description'].required = False
        self.fields['tax_rate'].required = False
        self.fields['mrp'].required = False
        self.fields['max_discount_pct'].required = False
        self.fields['atc_code'].required = False
        self.fields['storage_condition'].required = False
        self.fields['source_reference'].required = False
        self.fields['review_note'].required = False


class MedicineReviewForm(forms.ModelForm):
    """
    Form used by Pharmacist / Admin to review and complete a draft medicine catalogue entry.
    Requires essential fields before approving into active catalogue.
    """
    class Meta:
        model = Medicine
        fields = [
            'medicine_name', 'generic_name', 'brand_name', 'category',
            'dosage_form', 'strength', 'manufacturer', 'unit_of_measure',
            'regulatory_category', 'atc_code', 'storage_condition',
            'reorder_level', 'minimum_stock', 'maximum_stock', 'safety_stock',
            'tax_rate', 'mrp', 'max_discount_pct', 'review_note', 'description'
        ]
        widgets = {
            'medicine_name': forms.TextInput(attrs={'class': 'form-control'}),
            'generic_name': forms.TextInput(attrs={'class': 'form-control'}),
            'brand_name': forms.TextInput(attrs={'class': 'form-control'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'dosage_form': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Tablet, Syrup, Injection'}),
            'strength': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 500 mg, 10 mg/5ml'}),
            'manufacturer': forms.TextInput(attrs={'class': 'form-control'}),
            'unit_of_measure': forms.TextInput(attrs={'class': 'form-control'}),
            'regulatory_category': forms.Select(attrs={'class': 'form-select'}),
            'atc_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. N02BE01'}),
            'storage_condition': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Store below 25°C'}),
            'tax_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'mrp': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'max_discount_pct': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'reorder_level': forms.NumberInput(attrs={'class': 'form-control'}),
            'minimum_stock': forms.NumberInput(attrs={'class': 'form-control'}),
            'maximum_stock': forms.NumberInput(attrs={'class': 'form-control'}),
            'safety_stock': forms.NumberInput(attrs={'class': 'form-control'}),
            'review_note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = Category.objects.filter(is_active=True)
        self.fields['brand_name'].required = False
        self.fields['manufacturer'].required = False
        self.fields['atc_code'].required = False
        self.fields['storage_condition'].required = False
        self.fields['review_note'].required = False
        self.fields['description'].required = False
        self.fields['tax_rate'].required = False
        self.fields['mrp'].required = False
        self.fields['max_discount_pct'].required = False

    def clean_image(self):
        image = self.cleaned_data.get('image')
        if image:
            if hasattr(image, 'content_type') and image.content_type not in ALLOWED_IMAGE_TYPES:
                raise ValidationError('Only JPG, JPEG, PNG and WEBP images are allowed.')
            if image.size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                raise ValidationError(f'Image must not exceed {MAX_IMAGE_SIZE_MB} MB.')
        return image

    def clean_barcode(self):
        barcode = self.cleaned_data.get('barcode')
        if not barcode:
            return None
        qs = Medicine.objects.filter(barcode=barcode)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError('This barcode is already assigned to another medicine.')
        return barcode


class BatchForm(forms.ModelForm):
    class Meta:
        model = MedicineBatch
        fields = [
            'batch_number', 'manufacturing_date', 'expiry_date',
            'quantity_received', 'purchase_price', 'additional_cost',
            'selling_price', 'mrp', 'tax_rate', 'discount_pct',
            'storage_condition', 'is_controlled', 'status',
            'received_date'
        ]
        widgets = {
            'batch_number': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. B-123'
            }),
            'manufacturing_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'expiry_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'quantity_received': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'purchase_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'additional_cost': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'selling_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'mrp': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'tax_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '5.00'}),
            'discount_pct': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'storage_condition': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. Cool & dry place'
            }),
            'is_controlled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'status': forms.Select(attrs={'class': 'form-select'}),
            'received_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['manufacturing_date'].required = False
        self.fields['storage_condition'].required = False
        self.fields['additional_cost'].required = False
        self.fields['mrp'].required = False
        self.fields['discount_pct'].required = False

    def clean(self):
        cleaned_data = super().clean()
        mfg = cleaned_data.get('manufacturing_date')
        exp = cleaned_data.get('expiry_date')
        if mfg and exp and mfg >= exp:
            raise ValidationError('Manufacturing date cannot be on or after the expiry date.')
        purchase = cleaned_data.get('purchase_price')
        selling = cleaned_data.get('selling_price')
        add_cost = cleaned_data.get('additional_cost')
        mrp = cleaned_data.get('mrp')
        tax = cleaned_data.get('tax_rate')

        if purchase is not None and purchase < 0:
            raise ValidationError('Purchase price cannot be negative.')
        if selling is not None and selling < 0:
            raise ValidationError('Selling price cannot be negative.')
        if add_cost is not None and add_cost < 0:
            raise ValidationError('Additional cost cannot be negative.')
        if mrp is not None and mrp > 0 and selling is not None and selling > mrp:
            raise ValidationError('Selling price cannot exceed the Maximum Retail Price (MRP).')
        if tax is not None and (tax < 0 or tax > 100):
            raise ValidationError('Tax rate must be between 0% and 100%.')
        return cleaned_data


class BatchEditForm(forms.ModelForm):
    """
    Used exclusively in batch_edit_view.
    Excludes quantity_received (immutable historical receipt data) and
    quantity_available (must only change via StockService, never directly).
    Allows editing metadata: prices, costs, MRP, tax, storage, status, expiry dates.
    """
    class Meta:
        model = MedicineBatch
        fields = [
            'batch_number', 'manufacturing_date', 'expiry_date',
            'purchase_price', 'additional_cost', 'selling_price',
            'mrp', 'tax_rate', 'discount_pct',
            'storage_condition', 'is_controlled', 'status',
            'received_date'
        ]
        widgets = {
            'batch_number': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. B-123'
            }),
            'manufacturing_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'expiry_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'purchase_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'additional_cost': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'selling_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'mrp': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'tax_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '5.00'}),
            'discount_pct': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'storage_condition': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. Cool & dry place'
            }),
            'is_controlled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'status': forms.Select(attrs={'class': 'form-select'}),
            'received_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['manufacturing_date'].required = False
        self.fields['storage_condition'].required = False
        self.fields['additional_cost'].required = False
        self.fields['mrp'].required = False
        self.fields['discount_pct'].required = False

    def clean(self):
        cleaned_data = super().clean()
        mfg = cleaned_data.get('manufacturing_date')
        exp = cleaned_data.get('expiry_date')
        if mfg and exp and mfg >= exp:
            raise ValidationError('Manufacturing date cannot be on or after the expiry date.')
        purchase = cleaned_data.get('purchase_price')
        selling = cleaned_data.get('selling_price')
        add_cost = cleaned_data.get('additional_cost')
        mrp = cleaned_data.get('mrp')
        tax = cleaned_data.get('tax_rate')

        if purchase is not None and purchase < 0:
            raise ValidationError('Purchase price cannot be negative.')
        if selling is not None and selling < 0:
            raise ValidationError('Selling price cannot be negative.')
        if add_cost is not None and add_cost < 0:
            raise ValidationError('Additional cost cannot be negative.')
        if mrp is not None and mrp > 0 and selling is not None and selling > mrp:
            raise ValidationError('Selling price cannot exceed the Maximum Retail Price (MRP).')
        if tax is not None and (tax < 0 or tax > 100):
            raise ValidationError('Tax rate must be between 0% and 100%.')
        return cleaned_data


class MedicineValidationForm(forms.ModelForm):
    class Meta:
        model = Medicine
        fields = ['status']
        widgets = {
            'status': forms.Select(attrs={'class': 'form-select'}),
        }


class MedicineDeactivateForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label='I confirm I want to deactivate this medicine.',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )
