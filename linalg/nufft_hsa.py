"""
NUFFT HSA classes
=======================================
"""
from __future__ import absolute_import
import numpy
import scipy.sparse
import numpy.fft
import scipy.signal
import scipy.linalg
import scipy.special
from functools import wraps as _wraps

from ..src._helper import helper, helper1
class hypercube:
    def __init__(self, shape, steps, invsteps, nelements, batch, dtype):
        self.shape = shape
        self.steps = steps
        self.invsteps = invsteps
        self.nelements = nelements
        self.batch = batch
        self.dtype = dtype
    
        
def push_cuda_context(hsa_method):
    """
    Decorator: Push cude context to the top of the stack for current use
    Add @push_cuda_context before the methods of NUFFT_hsa()
    """
    @_wraps(hsa_method)
    def wrapper(*args, **kwargs):
        try:
            args[0].thr._context.push()
        except:
            pass     
        return hsa_method(*args, **kwargs)
    return wrapper

class NUFFT_hsa:
    """
    NUFFT_hsa class. 
    Multi-coil or single-coil memory reduced NUFFT. 

    """
    def __init__(self, API = None, platform_number=None, device_number=None):
        """
        Constructor.
        
        :param API: The API for the heterogeneous system. API='cuda' or API='ocl'
        :param platform_number: The number of the platform found by the API. 
        :param device_number: The number of the device found on the platform. 
        :type API: string
        :type platform_number: integer 
        :type device_number: integer 
        :returns: 0
        :rtype: int, float

        :Example:

        >>> import pynufft
        >>> NufftObj = pynufft.NUFFT_hsa(API='cuda', 0, 0)
        """
        
        self.dtype = numpy.complex64
   
        import reikna.cluda as cluda
        print('API = ', API)
        self.cuda_flag, self.ocl_flag = helper.diagnose()
        if None is API:
            if self.cuda_flag is 1:
                API = 'cuda'
            elif self.ocl_flag is 1:
                API = 'ocl'
            else:
                print('No accelerator is available.')
        else:
            api = API
        print('now using API = ', API)
        if platform_number is None:
            platform_number = 0
        if device_number is None:
            device_number = 0
        
        from reikna import cluda
        import reikna.transformations
        from reikna.cluda import functions, dtypes
        try: # try to create api/platform/device using the given parameters
            if 'cuda' == API:
                api = cluda.cuda_api()
            elif 'ocl' == API:
                api = cluda.ocl_api()
     
            platform = api.get_platforms()[platform_number]
            
            device = platform.get_devices()[device_number]
        except: # if failed, find out what's going wrong?
            print('No accelerator is detected.')
            
#             return 1

#         Create context from device
        self.thr = api.Thread(device) #pyopencl.create_some_context()
        self.device = device #: device name
        print('Using opencl or cuda = ', self.thr.api)

#         """
#         Wavefront: as warp in cuda. Can control the width in a workgroup
#         Wavefront is required in spmv_vector as it improves data coalescence.
#         see cCSR_spmv and zSparseMatVec
#         """
        self.wavefront = api.DeviceParameters(device).warp_size

        print('wavefront of OpenCL (as warp of CUDA) = ',self.wavefront)

        from ..src import re_subroutine #import create_kernel_sets
        kernel_sets = re_subroutine.create_kernel_sets(API)
               
        prg = self.thr.compile(kernel_sets, 
                                render_kwds=dict(LL =  str(self.wavefront)), 
                                fast_math=False)
        self.prg = prg        
        
        print("Note: In the future the api will change!")
        print("Note: NUFFT_hsa and NUFFT_cpu class will merge in the future!")
        print("You have been warned!")
    
    def plan(self, om, Nd, Kd, Jd, ft_axes = None, batch = None, radix = None):
        """
        Design the multi-coil or single-coil memory reduced interpolator. 
        
        
        :param om: The M off-grid locations in the frequency domain. Normalized between [-pi, pi]
        :param Nd: The matrix size of equispaced image. Example: Nd=(256,256) for a 2D image; Nd = (128,128,128) for a 3D image
        :param Kd: The matrix size of the oversampled frequency grid. Example: Kd=(512,512) for 2D image; Kd = (256,256,256) for a 3D image
        :param Jd: The interpolator size. Example: Jd=(6,6) for 2D image; Jd = (6,6,6) for a 3D image
        :param ft_axes: The dimensions to be transformed by FFT. Example: ft_axes = (0,1) for 2D, ft_axes = (0,1,2) for 3D; ft_axes = None for all dimensions.
        :param batch: Batch NUFFT. If provided, the shape is Nd + (batch, ). The last axes is the number of parallel coils. coil_sese = None for single coil. 
        :type om: numpy.float array, matrix size = (M, ndims)
        :type Nd: tuple, ndims integer elements. 
        :type Kd: tuple, ndims integer elements. 
        :type Jd: tuple, ndims integer elements. 
        :type ft_axes: tuple, selected axes to be transformed.
        :type batch: int or None
        :returns: 0
        :rtype: int, float
        :Example:

        >>> import pynufft
        >>> NufftObj = pynufft.NUFFT_cpu()
        >>> NufftObj.plan(om, Nd, Kd, Jd) 
        
        """         

        self.ndims = len(Nd) # dimension
        if ft_axes is None:
            ft_axes = range(0, self.ndims)
        self.ft_axes = ft_axes
#     
        self.st = helper.plan(om, Nd, Kd, Jd, ft_axes = ft_axes, format = 'pELL', radix = radix)
        if batch is None:
            self.parallel_flag = 0
        else:
            self.parallel_flag = 1
            
        if batch is None:
            self.batch = numpy.uint32(1)

        else:
            self.batch = numpy.uint32(batch)
 
        self.Nd = self.st['Nd']  # backup
        self.Kd = self.st['Kd']
#         self.sn = numpy.asarray(self.st['sn'].astype(self.dtype)  ,order='C')# backup
        if self.batch == 1 and (self.parallel_flag == 0):
            self.multi_Nd =   self.Nd
            self.multi_Kd =   self.Kd
            self.multi_M =   (self.st['M'], )      
#             self.sense2 = self.sense*numpy.reshape(self.sn, self.Nd + (1, )) # broadcasting the sense and scaling factor (Roll-off)
        else: #self.batch is 0:
            self.multi_Nd =   self.Nd + (self.batch, )
            self.multi_Kd =   self.Kd + (self.batch, )
            self.multi_M =   (self.st['M'], )+ (self.batch, )            
        self.invbatch = 1.0/self.batch
        self.Kdprod = numpy.uint32(numpy.prod(self.st['Kd']))
        self.Jdprod = numpy.uint32(numpy.prod(self.st['Jd']))
        self.Ndprod = numpy.uint32(numpy.prod(self.st['Nd']))
        
        self.Nd_elements, self.invNd_elements = helper.strides_divide_itemsize(self.st['Nd'])
        self.Kd_elements = helper.strides_divide_itemsize( self.st['Kd'])[0] # only return the Kd_elements
        self.NdCPUorder, self.KdCPUorder, self.nelem =     helper.preindex_copy(self.st['Nd'], self.st['Kd'])
        
        self.offload()
        
        return 0
    
    @push_cuda_context
    def offload(self):#API, platform_number=0, device_number=0):
        """
        self.offload():
        
        Off-load NUFFT to the opencl or cuda device(s)
        
        :param API: define the device type, which can be 'cuda' or 'ocl'
        :param platform_number: define which platform to be used. The default platform_number = 0.
        :param device_number: define which device to be used. The default device_number = 0.
        :type API: string
        :type platform_number: int
        :type device_number: int
        :return: self: instance
        """
        
        self.pELL = {} # dictionary
        
        self.pELL['nRow'] = numpy.uint32(self.st['pELL'].nRow)
        self.pELL['prodJd'] = numpy.uint32(self.st['pELL'].prodJd)
        self.pELL['sumJd'] = numpy.uint32(self.st['pELL'].sumJd)
        self.pELL['dim'] = numpy.uint32(self.st['pELL'].dim)
        self.pELL['Jd'] = self.thr.to_device(self.st['pELL'].Jd.astype(numpy.uint32))
        self.pELL['meshindex'] = self.thr.to_device(self.st['pELL'].meshindex.astype(numpy.uint32))
        self.pELL['kindx'] = self.thr.to_device(self.st['pELL'].kindx.astype(numpy.uint32))
        self.pELL['udata'] = self.thr.to_device(self.st['pELL'].udata.astype(self.dtype))
    
        self.volume = {}
        
        self.volume['Nd_elements'] = self.thr.to_device(numpy.asarray(self.Nd_elements, dtype = numpy.uint32))
        self.volume['Kd_elements'] = self.thr.to_device(numpy.asarray(self.Kd_elements, dtype = numpy.uint32))
        self.volume['invNd_elements'] = self.thr.to_device(self.invNd_elements.astype(numpy.float32))
        self.volume['Nd'] =  self.thr.to_device(numpy.asarray(self.st['Nd'], dtype = numpy.uint32))
        self.volume['NdGPUorder'] = self.thr.to_device( self.NdCPUorder)
        self.volume['KdGPUorder'] = self.thr.to_device( self.KdCPUorder)
        self.volume['gpu_coil_profile'] = self.thr.array(self.multi_Nd, dtype = self.dtype).fill(1.0)
        
        Nd = self.st['Nd']
#         tensor_sn = numpy.empty((numpy.sum(Nd), ), dtype=numpy.float32)
# 
#         shift = 0
#         for dimid in range(0, len(Nd)):
# 
#             tensor_sn[shift :shift + Nd[dimid]] = self.st['tensor_sn'][dimid][:,0].real
#             shift = shift + Nd[dimid]
#         self.volume['tensor_sn'] = self.thr.to_device(self.st['tensor_sn'].astype(numpy.float32))
        self.tSN = {}
        self.tSN['Td_elements'] = self.thr.to_device(numpy.asarray(self.st['tSN'].Td_elements, dtype = numpy.uint32))
        self.tSN['invTd_elements'] = self.thr.to_device(self.st['tSN'].invTd_elements.astype(numpy.float32))
        self.tSN['Td'] =  self.thr.to_device(numpy.asarray(self.st['tSN'].Td, dtype = numpy.uint32))
        self.tSN['Tdims'] = self.st['tSN'].Tdims
        self.tSN['tensor_sn'] = self.thr.to_device(self.st['tSN'].tensor_sn.astype(numpy.float32))
        
        self.Ndprod = numpy.int32(numpy.prod(self.st['Nd']))
        self.Kdprod = numpy.int32(numpy.prod(self.st['Kd']))
        self.M = numpy.int32(self.st['M'])
        

        import reikna.fft
        if self.batch > 1: # batch mode
            self.fft = reikna.fft.FFT(numpy.empty(self.st['Kd'] + (self.batch, ), dtype=self.dtype), self.ft_axes).compile(self.thr, fast_math=False)
        else: # elf.Reps ==1 Batch mode is wrong for 
            self.fft = reikna.fft.FFT(numpy.empty(self.st['Kd'], dtype=self.dtype), self.ft_axes).compile(self.thr, fast_math=False)

        self.zero_scalar=self.dtype(0.0+0.0j)
        del self.st['pELL']
        print('end of offload')
        
    @push_cuda_context
    def reset_sense(self):
        self.volume['gpu_coil_profile'].fill(1.0)
         
    @push_cuda_context
    def set_sense(self, coil_profile):
        if coil_profile.shape != self.multi_Nd:
            print('The shape of coil_profile is ', coil_profile.shape)
            print('But it should be', self.Nd + (self.batch, ))
            raise ValueError
        else:
            self.volume['gpu_coil_profile'] = self.thr.to_device(coil_profile.astype(self.dtype))
            print('Successfully loading coil sensitivities!')
        
#         if coil_profile.shape == self.Nd + (self.batch, ):        
        
        
        

        
    @push_cuda_context
    def to_device(self, image, shape = None):
        
        g_image = self.thr.array(image.shape, dtype = self.dtype)
        self.thr.to_device(image.astype(self.dtype), dest = g_image)
        return g_image
    
    @push_cuda_context
    def s2x(self, s):
        x = self.thr.array(self.multi_Nd, dtype=self.dtype)
#         print("Now populate the array to multi-coil")
        self.prg.cPopulate(self.batch, self.Ndprod, s, x, local_size = None, global_size = int(self.batch * self.Ndprod) )
#         x2 = x  *  self.volume['gpu_coil_profile']
#         try:
#             x2 = x  *  self.volume['gpu_coil_profile']
#         except:
#             x2 = x
        self.prg.cMultiplyVecInplace(numpy.uint32(1), self.volume['gpu_coil_profile'], x, local_size = None, global_size = int(self.batch * self.Ndprod) )
#         self.prg.cDistribute(self.batch, self.Ndprod, self.volume['gpu_coil_profile'], s, x,  local_size = None, global_size = int(self.batch * self.Ndprod) )
        return x
    
    @push_cuda_context                
    def x2xx(self, x):
#         xx = self.thr.array(xx.shape, dtype = self.dtype)
#         self.thr.copy_array(z, dest=xx, )#size = int(xx.nbytes/xx.dtype.itemsize)) #size = int(xx.nbytes/8) is a hack of error in cuda backends; 8 is the byte of numpy.complex64 
        xx = self.thr.array(x.shape, dtype = self.dtype)
        self.thr.copy_array(x, dest=xx, )#size = int(xx.nbytes/xx.dtype.itemsize)) #size = int(xx.nbytes/8) is a hack of error in cuda backends; 8 is the byte of numpy.complex64 
                
#         self.prg.cMultiplyRealInplace(self.batch, self.volume['SnGPUArray'], xx, local_size=None, global_size=int(self.Ndprod * self.batch))
#         self.prg.cTensorMultiply(numpy.uint32(self.batch), 
#                                     numpy.uint32(self.ndims),
#                                     self.volume['Nd'],
#                                     self.volume['Nd_elements'],
#                                     self.volume['invNd_elements'],
#                                     self.volume['tensor_sn'], 
#                                     xx, 
#                                     numpy.uint32(0),
#                                     local_size = None, global_size = int(self.batch * self.Ndprod))

        self.prg.cTensorMultiply(numpy.uint32(self.batch), 
                                            numpy.uint32(self.tSN['Tdims']),
                                            self.tSN['Td'],
                                            self.tSN['Td_elements'], 
                                            self.tSN['invTd_elements'], 
                                            self.tSN['tensor_sn'], 
                                            xx, 
                                            numpy.uint32(0),
                                            local_size = None, global_size = int(self.batch * self.Ndprod))
#         self.thr.synchronize()
        return xx
    
    @push_cuda_context
    def xx2k(self, xx):
        
        """
        Private: oversampled FFT on the heterogeneous device
        
        Firstly, zeroing the self.k_Kd array
        Second, copy self.x_Nd array to self.k_Kd array by cSelect
        Third: inplace FFT
        """
        k = self.thr.array(self.multi_Kd, dtype = self.dtype)#.fill(0.0 + 0.0j)
                    
        k.fill(0)
#         self.prg.cMultiplyScalar(self.zero_scalar, k, local_size=None, global_size=int(self.Kdprod))
#         self.prg.cSelect(self.NdGPUorder,      self.KdGPUorder,  xx, k, local_size=None, global_size=int(self.Ndprod))
#         self.prg.cSelect2(self.batch, self.volume['NdGPUorder'], self.volume['KdGPUorder'], xx, k, local_size = None, global_size = int(self.Ndprod * self.batch))
        self.prg.cTensorCopy(
                            self.batch, 
                            numpy.uint32(self.ndims), 
                             self.volume['Nd_elements'], 
                             self.volume['Kd_elements'], 
                             self.volume['invNd_elements'], 
                             xx, 
                             k, 
                             numpy.int32(1), # Directions: Nd -> Kd, 1; Kd -> Nd, -1
                             local_size = None, 
                             global_size = int(self.Ndprod))
        self.fft( k, k,inverse=False)
#         self.thr.synchronize()
        return k    
    
    @push_cuda_context
    def k2y(self, k):
        """
        Private: interpolation by the Sparse Matrix-Vector Multiplication
        """
#         if self.parallel_flag is 1:
#             y =self.thr.array( (self.st['M'], self.batch), dtype=self.dtype).fill(0)
#         else:
#             y =self.thr.array( (self.st['M'], ), dtype=self.dtype).fill(0)
        y =self.thr.array( self.multi_M, dtype=self.dtype).fill(0)
        self.prg.pELL_spmv_mCoil(
                            self.batch, 
                            self.pELL['nRow'],
                            self.pELL['prodJd'],
                            self.pELL['sumJd'], 
                            self.pELL['dim'],
                            self.pELL['Jd'],
#                             self.pELL_currsumJd,
                            self.pELL['meshindex'],
                            self.pELL['kindx'],
                            self.pELL['udata'], 
                            k,
                            y,
                            local_size= int(self.wavefront),
                            global_size= int(self.pELL['nRow'] * self.batch * self.wavefront)             
                            )           
#         self.thr.synchronize()
        return y

    @push_cuda_context
    def y2k(self, y):
        """
        Private: gridding by the Sparse Matrix-Vector Multiplication
        However, serial atomic add is far too slow and inaccurate.
        """

        kx = self.thr.array(self.multi_Kd, dtype = numpy.float32).fill(0.0)
        ky = self.thr.array(self.multi_Kd, dtype = numpy.float32).fill(0.0)
        
        self.prg.pELL_spmvh_mCoil(
                            self.batch, 
                            self.pELL['nRow'],
                            self.pELL['prodJd'],
                            self.pELL['sumJd'], 
                            self.pELL['dim'],
                            self.pELL['Jd'],
                            self.pELL['meshindex'],
                            self.pELL['kindx'],
                            self.pELL['udata'], 
                            kx, ky, 
                            y,
                            local_size=None,
                            global_size= int(self.pELL['nRow'] * self.pELL['prodJd']* self.batch)             
                            )         
        k = kx+1.0j* ky
        
        return k    

    @push_cuda_context
    def k2xx(self, k):
        """
        Private: the inverse FFT and image cropping (which is the reverse of _xx2k() method)
        """        
        
        self.fft( k, k, inverse=True)
#         self.thr.synchronize()
#         self.x_Nd._zero_fill()
#         self.prg.cMultiplyScalar(self.zero_scalar, xx,  local_size=None, global_size=int(self.Ndprod ))
#         if self.parallel_flag is 1:
#             xx = self.thr.array(self.st['Nd'] + (self.batch, ), dtype = self.dtype)
#         else:
#             xx = self.thr.array(self.st['Nd'], dtype = self.dtype)
        xx = self.thr.array(self.multi_Nd, dtype = self.dtype)
        xx.fill(0)
#         self.prg.cSelect(self.queue, (self.Ndprod,), None,   self.volume['KdGPUorder'].data,  self.NdGPUorder.data,     self.k_Kd2.data, self.x_Nd.data )
#         self.prg.cSelect2(self.batch,  self.volume['KdGPUorder'],  self.volume['NdGPUorder'],     k, xx, local_size=None, global_size=int(self.Ndprod * self.batch))
        self.prg.cTensorCopy(
                            self.batch, 
                            numpy.uint32(self.ndims), 
                             self.volume['Nd_elements'], 
                             self.volume['Kd_elements'], 
                             self.volume['invNd_elements'], 
                             k, 
                             xx, 
                             numpy.int32(-1),
                             local_size = None, 
                             global_size = int(self.Ndprod))        
        return xx
    
    @push_cuda_context
    def xx2x(self, xx):
        x = self.thr.array(xx.shape, dtype = self.dtype)
        self.thr.copy_array(xx, dest=x, )#size = int(xx.nbytes/xx.dtype.itemsize)) #size = int(xx.nbytes/8) is a hack of error in cuda backends; 8 is the byte of numpy.complex64 
        
#         self.prg.cMultiplyRealInplace(self.batch, self.volume['SnGPUArray'], z, local_size=None, global_size =  int(self.batch * self.Ndprod))
#         self.prg.cTensorMultiply(numpy.uint32(self.batch), 
#                                     numpy.uint32(self.ndims),
#                                     self.volume['Nd'],
#                                     self.volume['Nd_elements'],
#                                     self.volume['invNd_elements'],
#                                     self.volume['tensor_sn'], 
#                                     x, 
#                                     numpy.uint32(0),
#                                     local_size = None, global_size = int(self.batch * self.Ndprod))
        self.prg.cTensorMultiply(numpy.uint32(self.batch), 
                                            numpy.uint32(self.tSN['Tdims']),
                                            self.tSN['Td'],
                                            self.tSN['Td_elements'], 
                                            self.tSN['invTd_elements'], 
                                            self.tSN['tensor_sn'], 
                                            x, 
                                            numpy.uint32(0),
                                            local_size = None, global_size = int(self.batch * self.Ndprod))                                            
#         self.thr.synchronize()
        return x
            
    @push_cuda_context
    def x2s(self, x):
        s = self.thr.array(self.st['Nd'], dtype=self.dtype)
#         try:
        self.prg.cMultiplyConjVecInplace(numpy.uint32(1), self.volume['gpu_coil_profile'], x, local_size = None,  global_size = int(self.batch * self.Ndprod))
#         x2 = x  *  self.volume['gpu_coil_profile'].conj()
#         except:
#             x2 = x
        self.prg.cAggregate(self.batch, self.Ndprod, x, s, local_size = int(self.wavefront), global_size = int(self.batch * self.Ndprod * self.wavefront))
#         self.prg.cMerge(self.batch, self.Ndprod, self.volume['gpu_coil_profile'], x, s, local_size = int(self.wavefront), global_size = int(self.batch * self.Ndprod * self.wavefront))
        return s
        
    @push_cuda_context
    def selfadjoint_one2many2one(self, gx):
        """
        selfadjoint_one2many2one NUFFT (Teplitz) on the heterogeneous device
        
        :param gx: The input gpu array, with size=Nd
        :type: reikna gpu array with dtype =numpy.complex64
        :return: gx: The output gpu array, with size=Nd
        :rtype: reikna gpu array with dtype =numpy.complex64
        """      

        gy = self.forward_one2many(gx)
        gx2 = self.adjoint_many2one(gy)
        del gy
        return gx2    
    def selfadjoint(self, gx):
        """
        selfadjoint NUFFT (Teplitz) on the heterogeneous device
        
        :param gx: The input gpu array, with size=Nd
        :type: reikna gpu array with dtype =numpy.complex64
        :return: gx: The output gpu array, with size=Nd
        :rtype: reikna gpu array with dtype =numpy.complex64
        """      

        gy = self.forward(gx)
        gx2 = self.adjoint(gy)
        del gy
        return gx2
    
    @push_cuda_context
    def forward(self, gx):
        """
        Forward NUFFT on the heterogeneous device
        
        :param gx: The input gpu array, with size=Nd
        :type: reikna gpu array with dtype =numpy.complex64
        :return: gy: The output gpu array, with size=(M,)
        :rtype: reikna gpu array with dtype =numpy.complex64
        """        
        try:
            xx = self.x2xx(gx)
        except: # gx is not a gpu array 
            try:
                print('The input array may not be a GPUarray.')
                print('Automatically moving the input array to gpu, which is throttled by PCIe.')
                print('You have been warned!')
                px = self.to_device(gx, )
#                 pz = self.thr.to_device(numpy.asarray(gz.astype(self.dtype),  order = 'C' ))
                xx = self.x2xx(px)
            except:
                if gxx.shape != self.Nd + (self.batch, ):
                    print('shape of the input = ', gx.shape, ', but it should be ', self.Nd + (self.batch, ))
                raise
            
        k = self.xx2k(xx)
        del xx
        gy = self.k2y(k)
        del k
        return gy
    
    @push_cuda_context
    def forward_one2many(self, s):
        try:
            x = self.s2x(s)
        except: # gx is not a gpu array 
            try:
                print('In s2x(): The input array may not be a GPUarray.')
                print('Automatically moving the input array to gpu, which is throttled by PCIe.')
                print('You have been warned!')
                ps = self.to_device(s, )
#                 px = self.thr.to_device(numpy.asarray(x.astype(self.dtype),  order = 'C' ))
                x = self.s2x(ps)
            except:
                if s.shape != self.Nd:
                    print('shape of the input = ', x.shape, ', but it should be ', self.Nd)
                raise
        
        y = self.forward(x)
        return y
    
    @push_cuda_context
    def adjoint_many2one(self, y):
        try:
            x = self.adjoint(y)
        except: # gx is not a gpu array 
            try:
                print('y.shape = ', y.shape)
                print('In adjoint(): The input array may not be a GPUarray.')
                print('Automatically moving the input array to gpu, which is throttled by PCIe.')
                print('You have been warned!')
                py = self.to_device(y, )
#                 py = self.thr.to_device(numpy.asarray(y.astype(self.dtype),  order = 'C' ))
                x = self.adjoint(py)
            except:
                print('Failed at self.adjont! Please check the gy shape, type, stride.')
                raise        
#         z = self.adjoint(y)
        s = self.x2s(x)
        return s
        
    @push_cuda_context
    def adjoint(self, gy):
        """
        Adjoint NUFFT on the heterogeneous device
        
        :param gy: The input gpu array, with size=(M,)
        :type: reikna gpu array with dtype =numpy.complex64
        :return: gx: The output gpu array, with size=Nd
        :rtype: reikna gpu array with dtype =numpy.complex64
        """              
        try:
            k = self.y2k(gy)
        except: # gx is not a gpu array 
            try:
                print('In adjoint(): The input array may not be a GPUarray.')
                print('Automatically moving the input array to gpu, which is throttled by PCIe.')
                print('You have been warned!')
                py = self.to_device(gy, )
#                 py = self.thr.to_device(numpy.asarray(gy.astype(self.dtype),  order = 'C' ))
                k = self.y2k(py)
            except:
                print('Failed at self.adjont! Please check the gy shape, type, stride.')
                raise
                        
#             k = self.y2k(gy)
        xx = self.k2xx(k)
        del k
        gx = self.xx2x(xx)
        del xx
        return gx
    
    @push_cuda_context
    def release(self):
        del self.volume
        del self.prg
        del self.pELL
        self.thr.release()
        del self.thr
        
    @push_cuda_context
    def solve(self,gy, solver=None, *args, **kwargs):
        """
        The solver of NUFFT_hsa
        
        :param gy: data, reikna array, (M,) size
        :param solver: could be 'cg', 'L1TVOLS', 'L1TVLAD' 
        :param maxiter: the number of iterations
        :type gy: reikna array, dtype = numpy.complex64
        :type solver: string
        :type maxiter: int
        :return: reikna array with size Nd
        """
        from ..linalg.solve_hsa import solve
           
        try:
            return solve(self,  gy,  solver, *args, **kwargs)
        except:
            try:
                    print('In solve(): The input array may not be a GPUarray.')
                    print('Automatically moving the input array to gpu, which is throttled by PCIe.')
                    print('You have been warned!')
                    py = self.to_device(gy, )
                    return solve(self,  py,  solver, *args, **kwargs)
            except:
                if numpy.ndarray == type(gy):
                    print("input gy must be a reikna array with dtype = numpy.complex64")
                    raise #TypeError
                else:
                    print("wrong")
                    raise #TypeError
                

        

                   