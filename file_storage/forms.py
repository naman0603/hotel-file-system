from django import forms

class FileUploadForm(forms.Form):
    file = forms.FileField(
        label='Select a file',
        help_text='Max size: 100MB'
    )
    description = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Optional description'})
    )